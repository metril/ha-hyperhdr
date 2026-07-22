"""Tests for the reconnect supervisor (backoff/jitter) and the watchdog."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import DISCONNECT, FakeClientSession, FakeWebSocket, build_connect_script, wait_until

from custom_components.hyperhdr.client import HyperHdrServerClient
from custom_components.hyperhdr.const import WATCHDOG_INTERVAL
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
    _sleep; WATCHDOG_INTERVAL (15.0) never collides with a backoff value
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

        # No further frames arrive; after 2 watchdog cycles (15s each) the
        # connection is considered stale (30s stale_timeout) and closed.
        await wait_until(lambda: first_ws.close_calls >= 1)
        await wait_until(lambda: len(session.ws_connect_calls) == 2)
        await wait_until(lambda: client.connected)
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
