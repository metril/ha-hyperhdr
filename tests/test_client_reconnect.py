"""Tests for the reconnect supervisor (backoff/jitter) and the watchdog."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import (
    DISCONNECT,
    FakeClientSession,
    FakeWebSocket,
    build_connect_script,
    sysinfo_frame,
    wait_until,
)

from custom_components.hyperhdr.client import HyperHdrServerClient, _backoff_delay
from custom_components.hyperhdr.const import RECONNECT_MAX_DELAY, WATCHDOG_INTERVAL
from custom_components.hyperhdr.exceptions import HyperHdrConnectionError


class FakeClock:
    """An injectable virtual clock: sleep() advances monotonic() instantly."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def backoff_delays(clock: FakeClock) -> list[float]:
    """sleep_calls with watchdog-interval entries filtered out.

    The watchdog and the reconnect supervisor share the same injectable
    _sleep; WATCHDOG_INTERVAL (10.0) never collides with a backoff value
    (2, 4, 8, 16, 32, 60, 60, ...), so this filter cleanly isolates the
    backoff-purpose sleep calls.
    """
    return [s for s in clock.sleep_calls if abs(s - WATCHDOG_INTERVAL) > 1e-9]


def _make_client(
    session: FakeClientSession, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> HyperHdrServerClient:
    monkeypatch.setattr("custom_components.hyperhdr.client.random.uniform", lambda a, b: 1.0)
    client = HyperHdrServerClient(session, "localhost", 8090, **kwargs)  # type: ignore[arg-type]
    client._sleep = clock.sleep  # type: ignore[method-assign]
    client._monotonic = clock.monotonic  # type: ignore[method-assign]
    return client


class TestBackoffSequence:
    async def test_capped_backoff_sequence_with_no_jitter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = FakeClock()
        failures = [ConnectionRefusedError("refused")] * 7
        session = FakeClientSession(failures)
        client = _make_client(session, clock, monkeypatch)

        await client.start()
        await wait_until(lambda: len(backoff_delays(clock)) >= 7)
        await client.stop()

        assert backoff_delays(clock)[:7] == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]

    def test_delay_saturates_at_max_and_never_overflows_for_a_very_large_attempt_count(self) -> None:
        """Regression: ``RECONNECT_BASE_DELAY * (2**attempt)`` with an
        unclamped `attempt` eventually raises ``OverflowError`` converting
        an astronomically large int to float (found while testing the
        keepalive fix -- a scripted-fake-clock test that kept failing to
        reconnect burned through 1000+ attempts in a fraction of a real
        second and hit it). In real usage `attempt` grows roughly once per
        retry, reachable within a day or two of a continuous real-world
        outage -- this would permanently kill the reconnect supervisor task
        with nothing left to resurrect it.
        """
        assert _backoff_delay(0) == 2.0
        assert _backoff_delay(5) == RECONNECT_MAX_DELAY  # already saturated well before the clamp
        assert _backoff_delay(1_000_000) == RECONNECT_MAX_DELAY  # would have raised OverflowError pre-fix

    async def test_jitter_stays_within_plus_minus_20_percent(self) -> None:
        # No monkeypatch here -- exercise the real random.uniform jitter.
        clock = FakeClock()
        failures = [ConnectionRefusedError("refused")] * 2
        session = FakeClientSession(failures)
        client = HyperHdrServerClient(session, "localhost", 8090)  # type: ignore[arg-type]
        client._sleep = clock.sleep  # type: ignore[method-assign]
        client._monotonic = clock.monotonic  # type: ignore[method-assign]

        await client.start()
        await wait_until(lambda: len(backoff_delays(clock)) >= 1)
        await client.stop()

        first_delay = backoff_delays(clock)[0]
        assert 2.0 * 0.8 <= first_delay <= 2.0 * 1.2

    async def test_attempt_counter_resets_after_successful_connect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = FakeClock()
        # Attempt 1: fails outright. Attempt 2: connects fully then the
        # server disconnects. Attempt 3: fails outright again -- its delay
        # must restart from the base delay, not continue the progression.
        good_ws = FakeWebSocket([*build_connect_script(), DISCONNECT])
        sockets: list[Any] = [
            ConnectionRefusedError("refused"),
            good_ws,
            ConnectionRefusedError("refused"),
        ]
        session = FakeClientSession(sockets)
        client = _make_client(session, clock, monkeypatch)

        await client.start()
        await wait_until(lambda: len(backoff_delays(clock)) >= 3)
        await client.stop()

        delays = backoff_delays(clock)
        # attempt 1 fails at the base delay; attempt 2 connects successfully
        # then disconnects, so *its* post-disconnect delay is also the base
        # delay (the reset applies as of the successful connect); attempt 3
        # then fails again, continuing the (reset) progression to 4.0 --
        # NOT 8.0, which is what an un-reset counter would have produced.
        assert delays == [2.0, 2.0, 4.0]


class TestWatchdog:
    async def test_stale_connection_is_closed_and_reconnect_follows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = FakeClock()
        first_ws = FakeWebSocket(build_connect_script())
        second_ws = FakeWebSocket(build_connect_script())
        session = FakeClientSession([first_ws, second_ws])
        client = _make_client(session, clock, monkeypatch, stale_timeout=30.0)

        await client.start()
        await wait_until(lambda: client.connected)

        # No further frames arrive; after 3 watchdog cycles (WATCHDOG_INTERVAL
        # each) the connection is considered stale (30s stale_timeout) and
        # closed -- heartbeat defaults to 30.0 too, same as stale_timeout
        # here, so the stale check (checked first each tick) always wins and
        # no keepalive is ever sent before the close.
        await wait_until(lambda: first_ws.close_calls >= 1)
        await wait_until(lambda: len(session.ws_connect_calls) == 2)
        await wait_until(lambda: client.connected)
        await client.stop()


class TestKeepalive:
    async def test_idle_past_heartbeat_sends_sysinfo_keepalive_and_refreshes_last_rx(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FakeClock()
        ws = FakeWebSocket(build_connect_script())
        session = FakeClientSession(ws)
        client = _make_client(session, clock, monkeypatch, heartbeat=30.0, stale_timeout=90.0)

        await client.start()
        await wait_until(lambda: client.connected)

        before = len(ws.sent)
        await wait_until(lambda: len(ws.sent) > before)
        keepalive = ws.sent[-1]
        assert keepalive["command"] == "sysinfo"

        last_rx_before_ack = client._last_rx
        ws.push(sysinfo_frame(keepalive["tan"]))
        await wait_until(lambda: client._last_rx > last_rx_before_ack)
        assert ws.close_calls == 0
        assert client.connected

        # A second keepalive fires roughly one more `heartbeat` later --
        # proving the idle clock genuinely reset (not merely "didn't
        # crash") -- and the connection is STILL never force-closed despite
        # having been idle (module the two keepalive round-trips) for well
        # past what stale_timeout would have allowed without them.
        before2 = len(ws.sent)
        await wait_until(lambda: len(ws.sent) > before2)
        second_keepalive = ws.sent[-1]
        assert second_keepalive["command"] == "sysinfo"
        assert ws.close_calls == 0

        await client.stop()

    async def test_keepalive_timeout_is_swallowed_and_staleness_still_reconnects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A REAL (tiny) request_timeout here -- unlike `_sleep`/`_monotonic`,
        # `_send_command`'s own asyncio.wait_for is bound to the real event
        # loop clock, not the injectable one. Keeping it in the low
        # milliseconds is what makes exercising a genuine keepalive timeout
        # here practical without an actually-slow test. A `success: false`
        # response would NOT exercise this path -- ANY received frame
        # (error or not) still refreshes `_last_rx` in `_receive_loop`, so
        # only a response that never arrives at all leaves it stale.
        clock = FakeClock()
        first_ws = FakeWebSocket(build_connect_script())
        # Nothing ever responds to (or otherwise refreshes) the reconnected
        # client's own idle connection either, so it cycles again the same
        # way -- a few spare sockets keep that from starving the fake
        # session before the assertions below get a chance to stop() it.
        spare_sockets = [FakeWebSocket(build_connect_script()) for _ in range(4)]
        session = FakeClientSession([first_ws, *spare_sockets])
        # heartbeat is deliberately 2x WATCHDOG_INTERVAL (not 1x) here: with
        # the fake clock's near-instant sleep(), the watchdog's very FIRST
        # tick can otherwise race ahead of the connect handshake itself
        # (still in flight, real scheduling turns not yet exhausted) --
        # observed to send a keepalive that steals a tan the handshake's
        # own in-flight `serverinfo` call was about to use, corrupting this
        # test's pre-scripted response sequence. Giving the handshake a
        # full extra tick of headroom avoids that without weakening what
        # this test actually verifies.
        client = _make_client(
            session, clock, monkeypatch, heartbeat=20.0, stale_timeout=30.0, request_timeout=0.1
        )

        await client.start()
        await wait_until(lambda: client.connected)

        before = len(first_ws.sent)
        await wait_until(lambda: len(first_ws.sent) > before)
        keepalive = first_ws.sent[-1]
        assert keepalive["command"] == "sysinfo"
        # Deliberately never respond to it.

        # The timed-out keepalive must not crash the watchdog task (nor
        # force an immediate close itself) -- it's swallowed, and staleness
        # (last_rx never refreshed, since no response ever arrived) is what
        # eventually forces the reconnect: the "normal error path", not a
        # bespoke one. (Not asserting `client.connected` afterwards -- the
        # reconnected client is just as idle, so it may already be cycling
        # again by the time this checks; the reconnect attempt itself is
        # what this test is verifying.)
        await wait_until(lambda: first_ws.close_calls >= 1, timeout=3.0)
        await wait_until(lambda: len(session.ws_connect_calls) >= 2)
        await client.stop()

    async def test_active_traffic_suppresses_keepalive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A fresh push frame is injected as part of the SAME injected
        # `_sleep` call the watchdog itself awaits every tick -- deliberately
        # avoiding a second, independently-scheduled task racing the
        # watchdog's own cooperative scheduling (which a naive "poll
        # clock.sleep_calls from the test, then push" loop is prone to: if
        # the watchdog ever ticks ahead before the test's own turn, a real
        # keepalive fires and blocks on a real request_timeout).
        first_ws_holder: list[FakeWebSocket] = []

        class _TrafficClock(FakeClock):
            async def sleep(self, seconds: float) -> None:
                await super().sleep(seconds)
                first_ws_holder[0].push({"command": "components-update", "data": {"name": "SMOOTHING", "enabled": True}})
                # A couple of extra cooperative turns so the receive loop
                # actually consumes the just-pushed frame (stamping
                # `_last_rx`) before the watchdog's own next line re-checks
                # idle time.
                await asyncio.sleep(0)
                await asyncio.sleep(0)

        clock = _TrafficClock()
        ws = FakeWebSocket(build_connect_script())
        first_ws_holder.append(ws)
        session = FakeClientSession(ws)
        client = _make_client(session, clock, monkeypatch, heartbeat=10.0, stale_timeout=1000.0, request_timeout=0.01)

        await client.start()
        await wait_until(lambda: client.connected)
        baseline_sent = len(ws.sent)

        await wait_until(lambda: len(clock.sleep_calls) >= 8)
        assert len(ws.sent) == baseline_sent  # no keepalive was ever sent -- traffic kept last_rx fresh
        await client.stop()


class TestStopAndMidRequestDisconnect:
    async def test_stop_cancels_cleanly_with_no_pending_futures(self) -> None:
        ws = FakeWebSocket(build_connect_script())
        session = FakeClientSession(ws)
        client = HyperHdrServerClient(session, "localhost", 8090)  # type: ignore[arg-type]

        await client.start()
        await wait_until(lambda: client.connected)
        await client.stop()

        assert client._pending == {}
        assert not client.connected

    async def test_pending_futures_get_connection_error_when_socket_drops_mid_request(self) -> None:
        ws = FakeWebSocket(build_connect_script())
        session = FakeClientSession(ws)
        client = HyperHdrServerClient(session, "localhost", 8090)  # type: ignore[arg-type]

        await client.start()
        await wait_until(lambda: client.connected)

        task = asyncio.ensure_future(client._send_command("sysinfo"))
        await wait_until(lambda: len(ws.sent) >= 3)
        ws.push(DISCONNECT)

        with pytest.raises(HyperHdrConnectionError):
            await task
        await client.stop()


class TestStartInstanceSyntheticPush:
    async def test_start_instance_success_triggers_synthetic_roster_refresh(self) -> None:
        ws = FakeWebSocket(build_connect_script(admin_password="hyperhdr"))
        session = FakeClientSession(ws)
        client = HyperHdrServerClient(session, "localhost", 8090, admin_password="hyperhdr")  # type: ignore[arg-type]

        pushes: list[dict[str, Any]] = []
        client.set_push_callback("instance-update", pushes.append)

        await client.start()
        await wait_until(lambda: client.connected)
        assert client.admin_logged_in is True

        task = asyncio.ensure_future(client.start_instance(1))
        await wait_until(lambda: len(ws.sent) >= 4)
        start_tan = ws.sent[-1]["tan"]
        assert ws.sent[-1]["subcommand"] == "startInstance"
        ws.push({"command": "instance-startInstance", "success": True, "tan": start_tan})

        await wait_until(lambda: len(ws.sent) >= 5)
        serverinfo_tan = ws.sent[-1]["tan"]
        roster = [
            {"instance": 0, "friendly_name": "First LED instance", "running": True},
            {"instance": 1, "friendly_name": "Second", "running": True},
        ]
        ws.push({"command": "serverinfo", "success": True, "tan": serverinfo_tan, "info": {"instance": roster}})

        await task
        assert len(pushes) == 1
        assert pushes[0]["command"] == "instance-update"
        assert pushes[0]["data"] == roster
        await client.stop()

    async def test_create_instance_sends_name_and_triggers_synthetic_roster_refresh(self) -> None:
        ws = FakeWebSocket(build_connect_script(admin_password="hyperhdr"))
        session = FakeClientSession(ws)
        client = HyperHdrServerClient(session, "localhost", 8090, admin_password="hyperhdr")  # type: ignore[arg-type]

        pushes: list[dict[str, Any]] = []
        client.set_push_callback("instance-update", pushes.append)

        await client.start()
        await wait_until(lambda: client.connected)

        task = asyncio.ensure_future(client.create_instance("New Instance"))
        await wait_until(lambda: len(ws.sent) >= 4)
        create_sent = ws.sent[-1]
        assert create_sent["command"] == "instance"
        assert create_sent["subcommand"] == "createInstance"
        assert create_sent["name"] == "New Instance"
        ws.push({"command": "instance-createInstance", "success": True, "tan": create_sent["tan"]})

        await wait_until(lambda: len(ws.sent) >= 5)
        serverinfo_tan = ws.sent[-1]["tan"]
        roster = [
            {"instance": 0, "friendly_name": "First LED instance", "running": True},
            {"instance": 1, "friendly_name": "New Instance", "running": False},
        ]
        ws.push({"command": "serverinfo", "success": True, "tan": serverinfo_tan, "info": {"instance": roster}})

        await task
        assert len(pushes) == 1
        assert pushes[0]["data"] == roster
        await client.stop()

    async def test_delete_instance_sends_instance_id_and_triggers_synthetic_roster_refresh(self) -> None:
        ws = FakeWebSocket(build_connect_script(admin_password="hyperhdr"))
        session = FakeClientSession(ws)
        client = HyperHdrServerClient(session, "localhost", 8090, admin_password="hyperhdr")  # type: ignore[arg-type]

        pushes: list[dict[str, Any]] = []
        client.set_push_callback("instance-update", pushes.append)

        await client.start()
        await wait_until(lambda: client.connected)

        task = asyncio.ensure_future(client.delete_instance(1))
        await wait_until(lambda: len(ws.sent) >= 4)
        delete_sent = ws.sent[-1]
        assert delete_sent["command"] == "instance"
        assert delete_sent["subcommand"] == "deleteInstance"
        assert delete_sent["instance"] == 1
        ws.push({"command": "instance-deleteInstance", "success": True, "tan": delete_sent["tan"]})

        await wait_until(lambda: len(ws.sent) >= 5)
        serverinfo_tan = ws.sent[-1]["tan"]
        roster = [{"instance": 0, "friendly_name": "First LED instance", "running": True}]
        ws.push({"command": "serverinfo", "success": True, "tan": serverinfo_tan, "info": {"instance": roster}})

        await task
        assert len(pushes) == 1
        assert pushes[0]["data"] == roster
        await client.stop()
