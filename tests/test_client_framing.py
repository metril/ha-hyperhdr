"""Tests for tan correlation, push routing, and malformed-frame handling."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import (
    FakeClientSession,
    FakeWebSocket,
    build_connect_script,
    error_response_frame,
    ledstream_update_frame,
    load_fixture,
    videomodehdr_response_frame,
    wait_until,
)

from custom_components.hyperhdr.client import HyperHdrInstanceClient, HyperHdrServerClient
from custom_components.hyperhdr.const import IMAGESTREAM_UPDATE_TOPIC, LEDSTREAM_UPDATE_TOPIC
from custom_components.hyperhdr.exceptions import HyperHdrApiError, HyperHdrAuthError, HyperHdrConnectionError


async def _connected_server_client(ws: FakeWebSocket) -> HyperHdrServerClient:
    session = FakeClientSession(ws)
    client = HyperHdrServerClient(session, "localhost", 8090, request_timeout=0.2)  # type: ignore[arg-type]
    await client.start()
    await wait_until(lambda: client.connected)
    return client


@pytest.fixture
async def connected(request: pytest.FixtureRequest) -> Any:
    ws = FakeWebSocket(build_connect_script())
    client = await _connected_server_client(ws)
    yield client, ws
    await client.stop()


@pytest.fixture
async def connected_instance(request: pytest.FixtureRequest) -> Any:
    ws = FakeWebSocket(build_connect_script(is_instance=True, instance_id=0))
    session = FakeClientSession(ws)
    client = HyperHdrInstanceClient(session, "localhost", 8090, instance_id=0, request_timeout=0.2)  # type: ignore[arg-type]
    await client.start()
    await wait_until(lambda: client.connected)
    yield client, ws
    await client.stop()


class TestTanCorrelation:
    async def test_tan_increments_per_request(self, connected: tuple[HyperHdrServerClient, FakeWebSocket]) -> None:
        client, ws = connected

        task1 = asyncio.ensure_future(client._send_command("sysinfo"))
        await wait_until(lambda: len(ws.sent) >= 3)
        assert ws.sent[-1]["tan"] == 3
        ws.push({"command": "sysinfo", "success": True, "tan": 3, "info": {}})
        await task1

        task2 = asyncio.ensure_future(client._send_command("sysinfo"))
        await wait_until(lambda: len(ws.sent) >= 4)
        assert ws.sent[-1]["tan"] == 4
        ws.push({"command": "sysinfo", "success": True, "tan": 4, "info": {}})
        await task2

    async def test_out_of_order_responses_resolve_correct_futures(
        self, connected: tuple[HyperHdrServerClient, FakeWebSocket]
    ) -> None:
        client, ws = connected

        task_a = asyncio.ensure_future(client._send_command("sysinfo"))
        await wait_until(lambda: len(ws.sent) >= 3)
        tan_a = ws.sent[-1]["tan"]

        task_b = asyncio.ensure_future(client._send_command("sysinfo"))
        await wait_until(lambda: len(ws.sent) >= 4)
        tan_b = ws.sent[-1]["tan"]

        assert tan_b == tan_a + 1

        # Respond to B first, then A -- each task must still get ITS response.
        ws.push({"command": "sysinfo", "success": True, "tan": tan_b, "info": {"marker": "B"}})
        ws.push({"command": "sysinfo", "success": True, "tan": tan_a, "info": {"marker": "A"}})

        result_a = await task_a
        result_b = await task_b
        assert result_a["info"]["marker"] == "A"
        assert result_b["info"]["marker"] == "B"

    async def test_concurrent_in_flight_requests(self, connected: tuple[HyperHdrServerClient, FakeWebSocket]) -> None:
        client, ws = connected

        task_a = asyncio.ensure_future(client._send_command("sysinfo"))
        task_b = asyncio.ensure_future(client._send_command("sysinfo"))
        task_c = asyncio.ensure_future(client._send_command("sysinfo"))
        await wait_until(lambda: len(ws.sent) >= 5)

        tans = [m["tan"] for m in ws.sent[-3:]]
        assert tans == sorted(tans)
        assert len(set(tans)) == 3

        for tan in tans:
            ws.push({"command": "sysinfo", "success": True, "tan": tan, "info": {"tan": tan}})

        results = await asyncio.gather(task_a, task_b, task_c)
        assert {r["info"]["tan"] for r in results} == set(tans)


class TestPushRouting:
    async def test_update_message_routes_to_push_callback_not_a_future(
        self, connected: tuple[HyperHdrServerClient, FakeWebSocket]
    ) -> None:
        client, ws = connected
        received: list[dict[str, Any]] = []
        client.set_push_callback("instance-update", received.append)

        # Real capture of the roster push HyperHDR sends after an instance
        # is stopped (see docs/api-notes.md and instance_update_stopped.json).
        push_msg = load_fixture("instance_update_stopped.json")
        ws.push(push_msg)
        await wait_until(lambda: len(received) == 1)
        assert received[0] == push_msg

    async def test_ledstream_frame_tan_collision_does_not_corrupt_pending_request(
        self, connected: tuple[HyperHdrServerClient, FakeWebSocket]
    ) -> None:
        """A push frame that reuses a pending request's tan must never
        resolve that request's future -- the request must still get its
        own real response later."""
        client, ws = connected
        frames: list[dict[str, Any]] = []
        client.set_push_callback(LEDSTREAM_UPDATE_TOPIC, frames.append)

        pending_task = asyncio.ensure_future(client._send_command("sysinfo"))
        await wait_until(lambda: len(ws.sent) >= 3)
        colliding_tan = ws.sent[-1]["tan"]

        # A ledstream frame arrives reusing the *same* tan as the pending request.
        ws.push(ledstream_update_frame(colliding_tan))
        await wait_until(lambda: len(frames) == 1)
        assert frames[0]["tan"] == colliding_tan

        # The pending request must NOT have been resolved by the colliding frame.
        assert not pending_task.done()

        # Its real response (same tan) now arrives and must resolve it correctly.
        ws.push({"command": "sysinfo", "success": True, "tan": colliding_tan, "info": {"real": True}})
        result = await pending_task
        assert result["info"] == {"real": True}


class TestMalformedAndUnmatchedFrames:
    async def test_unmatched_tan_dropped_without_error_and_counted(
        self, connected: tuple[HyperHdrServerClient, FakeWebSocket]
    ) -> None:
        client, ws = connected
        before = client.malformed_or_unmatched_count
        ws.push({"command": "sysinfo", "success": True, "tan": 9999, "info": {}})
        await wait_until(lambda: client.malformed_or_unmatched_count == before + 1)
        assert client.connected  # loop must still be alive

    async def test_malformed_json_frame_dropped_and_loop_continues(
        self, connected: tuple[HyperHdrServerClient, FakeWebSocket]
    ) -> None:
        client, ws = connected
        before = client.malformed_or_unmatched_count
        ws.push("{not valid json")
        await wait_until(lambda: client.malformed_or_unmatched_count == before + 1)

        # Loop must still be alive and able to correlate a subsequent request.
        task = asyncio.ensure_future(client._send_command("sysinfo"))
        await wait_until(lambda: len(ws.sent) >= 3)
        tan = ws.sent[-1]["tan"]
        ws.push({"command": "sysinfo", "success": True, "tan": tan, "info": {}})
        await task

    async def test_non_dict_json_frame_dropped_and_counted(
        self, connected: tuple[HyperHdrServerClient, FakeWebSocket]
    ) -> None:
        client, ws = connected
        before = client.malformed_or_unmatched_count
        ws.push("[1, 2, 3]")
        await wait_until(lambda: client.malformed_or_unmatched_count == before + 1)


class TestCommandErrors:
    async def test_success_false_raises_api_error_with_real_error_response_fixture(
        self, connected: tuple[HyperHdrServerClient, FakeWebSocket]
    ) -> None:
        """Real error_response.json capture: an unrecognized command comes
        back with the FULL server error text (not a truncated hand-typed
        copy) AND an empty ``command`` field. The pending future must still
        resolve -- proving correlation is by `tan` alone, never by command
        name (see docs/api-notes.md)."""
        client, ws = connected
        task = asyncio.ensure_future(client._send_command("totallyBogusCommand"))
        await wait_until(lambda: len(ws.sent) >= 3)
        tan = ws.sent[-1]["tan"]
        ws.push(error_response_frame(tan))

        with pytest.raises(HyperHdrApiError) as excinfo:
            await task
        assert excinfo.value.command == ""
        assert excinfo.value.error == "Errors during message validation, please consult the HyperHDR Log."

    async def test_raise_on_error_false_returns_raw_dict_on_failure(
        self, connected: tuple[HyperHdrServerClient, FakeWebSocket]
    ) -> None:
        client, ws = connected
        task = asyncio.ensure_future(client._send_command("authorize", subcommand="login", raise_on_error=False))
        await wait_until(lambda: len(ws.sent) >= 3)
        tan = ws.sent[-1]["tan"]
        ws.push({"command": "authorize", "success": False, "tan": tan, "error": "No Authorization"})
        result = await task
        assert result["success"] is False
        assert result["error"] == "No Authorization"

    async def test_timeout_raises_connection_error_and_clears_pending_entry(
        self, connected: tuple[HyperHdrServerClient, FakeWebSocket]
    ) -> None:
        client, ws = connected
        with pytest.raises(HyperHdrConnectionError):
            await client._send_command("sysinfo", timeout=0.05)

        # The stale tan must no longer be in the pending map -- a late
        # response for it should be dropped as unmatched, not resolve
        # a future no one is awaiting anymore.
        before = client.malformed_or_unmatched_count
        tan = ws.sent[-1]["tan"]
        ws.push({"command": "sysinfo", "success": True, "tan": tan, "info": {}})
        await wait_until(lambda: client.malformed_or_unmatched_count == before + 1)


class TestOnConnectedCallbackForms:
    """on_connected accepts either a sync callable or a coroutine function."""

    async def test_sync_on_connected_receives_the_snapshot_info_dict(self) -> None:
        ws = FakeWebSocket(build_connect_script(serverinfo_info={"marker": "sync"}))
        session = FakeClientSession(ws)
        client = HyperHdrServerClient(session, "localhost", 8090)  # type: ignore[arg-type]
        received: list[dict[str, Any]] = []
        client.on_connected = received.append

        await client.start()
        await wait_until(lambda: client.connected)
        assert received == [{"marker": "sync"}]
        await client.stop()

    async def test_async_on_connected_is_awaited(self) -> None:
        ws = FakeWebSocket(build_connect_script(serverinfo_info={"marker": "async"}))
        session = FakeClientSession(ws)
        client = HyperHdrServerClient(session, "localhost", 8090)  # type: ignore[arg-type]
        received: list[dict[str, Any]] = []

        async def _on_connected(info: dict[str, Any]) -> None:
            await asyncio.sleep(0)
            received.append(info)

        client.on_connected = _on_connected

        await client.start()
        await wait_until(lambda: len(received) == 1)
        assert received == [{"marker": "async"}]
        await client.stop()


async def _wait_and_ack(ws: FakeWebSocket, before: int) -> dict[str, Any]:
    """Wait for a new send past `before`, then acknowledge it (success: true)."""
    await wait_until(lambda: len(ws.sent) > before)
    sent = ws.sent[-1]
    ws.push({"command": sent["command"], "success": True, "tan": sent["tan"]})
    return sent


class TestInstanceClientCommandPayloads:
    """Verify the exact wire payload sent for each instance command method."""

    async def test_async_set_color(self, connected_instance: tuple[HyperHdrInstanceClient, FakeWebSocket]) -> None:
        client, ws = connected_instance
        before = len(ws.sent)
        task = asyncio.ensure_future(client.async_set_color((10, 20, 30), priority=128, duration_ms=500))
        sent = await _wait_and_ack(ws, before)
        await task
        assert sent["command"] == "color"
        assert sent["color"] == [10, 20, 30]
        assert sent["priority"] == 128
        assert sent["duration"] == 500
        assert sent["origin"] == "Home Assistant"

    async def test_async_set_effect(self, connected_instance: tuple[HyperHdrInstanceClient, FakeWebSocket]) -> None:
        client, ws = connected_instance
        before = len(ws.sent)
        task = asyncio.ensure_future(client.async_set_effect("Rainbow swirl fast", priority=150))
        sent = await _wait_and_ack(ws, before)
        await task
        assert sent["command"] == "effect"
        assert sent["effect"] == {"name": "Rainbow swirl fast"}
        assert sent["priority"] == 150
        assert sent["duration"] == 0

    async def test_async_clear(self, connected_instance: tuple[HyperHdrInstanceClient, FakeWebSocket]) -> None:
        client, ws = connected_instance
        before = len(ws.sent)
        task = asyncio.ensure_future(client.async_clear(-1))
        sent = await _wait_and_ack(ws, before)
        await task
        assert sent == {"command": "clear", "tan": sent["tan"], "priority": -1}

    async def test_async_set_component(self, connected_instance: tuple[HyperHdrInstanceClient, FakeWebSocket]) -> None:
        client, ws = connected_instance
        before = len(ws.sent)
        task = asyncio.ensure_future(client.async_set_component("SMOOTHING", False))
        sent = await _wait_and_ack(ws, before)
        await task
        assert sent["command"] == "componentstate"
        assert sent["componentstate"] == {"component": "SMOOTHING", "state": False}

    async def test_async_set_adjustment_maps_only_provided_fields(
        self, connected_instance: tuple[HyperHdrInstanceClient, FakeWebSocket]
    ) -> None:
        client, ws = connected_instance
        before = len(ws.sent)
        task = asyncio.ensure_future(client.async_set_adjustment(luminance_gain=0.8, gamma=1.2))
        sent = await _wait_and_ack(ws, before)
        await task
        assert sent["command"] == "adjustment"
        # ONLY the two provided fields, camelCase-mapped -- never the full
        # cached adjustment object echoed back (the reference bug class).
        assert sent["adjustment"] == {"luminanceGain": 0.8, "gamma": 1.2}

    async def test_async_set_adjustment_unknown_field_raises(
        self, connected_instance: tuple[HyperHdrInstanceClient, FakeWebSocket]
    ) -> None:
        client, ws = connected_instance
        with pytest.raises(ValueError, match="unknown adjustment field"):
            await client.async_set_adjustment(brightness=80)
        # Nothing must have been sent for the rejected field.
        assert all(m.get("command") != "adjustment" for m in ws.sent)

    async def test_async_set_adjustment_no_fields_is_a_noop(
        self, connected_instance: tuple[HyperHdrInstanceClient, FakeWebSocket]
    ) -> None:
        client, ws = connected_instance
        before = len(ws.sent)
        await client.async_set_adjustment()
        assert len(ws.sent) == before

    async def test_async_set_hdr_mode(self, connected_instance: tuple[HyperHdrInstanceClient, FakeWebSocket]) -> None:
        client, ws = connected_instance
        before = len(ws.sent)
        task = asyncio.ensure_future(client.async_set_hdr_mode(1))
        await wait_until(lambda: len(ws.sent) > before)
        sent = ws.sent[-1]
        # Real videomodehdr_response.json capture -- a bare success ack, no `info`.
        ws.push(videomodehdr_response_frame(sent["tan"]))
        await task
        assert sent["command"] == "videomodehdr"
        assert sent["HDR"] == 1

    async def test_async_select_source_with_priority(
        self, connected_instance: tuple[HyperHdrInstanceClient, FakeWebSocket]
    ) -> None:
        client, ws = connected_instance
        before = len(ws.sent)
        task = asyncio.ensure_future(client.async_select_source(128))
        sent = await _wait_and_ack(ws, before)
        await task
        assert sent["command"] == "sourceselect"
        assert sent["priority"] == 128
        assert "auto" not in sent

    async def test_async_select_source_auto(
        self, connected_instance: tuple[HyperHdrInstanceClient, FakeWebSocket]
    ) -> None:
        client, ws = connected_instance
        before = len(ws.sent)
        task = asyncio.ensure_future(client.async_select_source(None))
        sent = await _wait_and_ack(ws, before)
        await task
        assert sent["command"] == "sourceselect"
        assert sent["auto"] is True
        assert "priority" not in sent


class TestLedstreamRefcounting:
    async def test_start_ledstream_sends_start_only_on_first_consumer(
        self, connected_instance: tuple[HyperHdrInstanceClient, FakeWebSocket]
    ) -> None:
        client, ws = connected_instance
        frames: list[dict[str, Any]] = []

        before = len(ws.sent)
        task1 = asyncio.ensure_future(client.start_ledstream(frames.append))
        sent = await _wait_and_ack(ws, before)
        await task1
        assert sent["command"] == "ledcolors"
        assert sent["subcommand"] == "ledstream-start"

        sent_before_second = len(ws.sent)
        await client.start_ledstream(frames.append)
        assert len(ws.sent) == sent_before_second  # second consumer: no new send

        # A pushed frame is routed to the registered callback.
        ws.push(ledstream_update_frame(sent["tan"]))
        await wait_until(lambda: len(frames) == 1)

    async def test_stop_ledstream_only_sends_stop_at_refcount_zero(
        self, connected_instance: tuple[HyperHdrInstanceClient, FakeWebSocket]
    ) -> None:
        client, ws = connected_instance
        frames: list[dict[str, Any]] = []

        before = len(ws.sent)
        t1 = asyncio.ensure_future(client.start_ledstream(frames.append))
        await _wait_and_ack(ws, before)
        await t1
        await client.start_ledstream(frames.append)  # refcount now 2

        sent_before = len(ws.sent)
        await client.stop_ledstream(frames.append)  # refcount -> 1, no stop sent
        assert len(ws.sent) == sent_before

        t2 = asyncio.ensure_future(client.stop_ledstream(frames.append))  # refcount -> 0
        sent = await _wait_and_ack(ws, sent_before)
        await t2
        assert sent["command"] == "ledcolors"
        assert sent["subcommand"] == "ledstream-stop"

        # Callback unregistered at refcount zero -- a further frame is
        # silently dropped in _dispatch_push (still routed as a push per
        # routing rule (a), just with no registered handler -- this does
        # NOT bump malformed_or_unmatched_count, which is reserved for
        # unmatched-tan/malformed frames). Send another real request
        # afterwards and confirm it still resolves normally, proving the
        # loop stayed healthy and `frames` was never appended to.
        ws.push(ledstream_update_frame(sent["tan"]))
        sent_before_probe = len(ws.sent)
        probe_task = asyncio.ensure_future(client.async_clear(-1))
        await _wait_and_ack(ws, sent_before_probe)
        await probe_task
        assert len(frames) == 0


class TestImagestreamRequiresAdmin:
    async def test_start_imagestream_without_admin_login_raises(
        self, connected_instance: tuple[HyperHdrInstanceClient, FakeWebSocket]
    ) -> None:
        client, ws = connected_instance
        assert client.admin_logged_in is False
        sent_before = len(ws.sent)
        with pytest.raises(HyperHdrAuthError):
            await client.start_imagestream(lambda data: None)
        assert len(ws.sent) == sent_before

    async def test_start_imagestream_with_admin_login_sends_start(self) -> None:
        ws = FakeWebSocket(build_connect_script(is_instance=True, instance_id=0, admin_password="hyperhdr"))
        session = FakeClientSession(ws)
        client = HyperHdrInstanceClient(
            session,  # type: ignore[arg-type]
            "localhost",
            8090,
            instance_id=0,
            admin_password="hyperhdr",
            request_timeout=0.2,
        )
        await client.start()
        await wait_until(lambda: client.connected)
        assert client.admin_logged_in is True

        frames: list[dict[str, Any]] = []
        before = len(ws.sent)
        task = asyncio.ensure_future(client.start_imagestream(frames.append))
        sent = await _wait_and_ack(ws, before)
        await task
        assert sent["command"] == "ledcolors"
        assert sent["subcommand"] == "imagestream-start"

        ws.push({"command": IMAGESTREAM_UPDATE_TOPIC, "result": {}, "success": True, "tan": sent["tan"]})
        await wait_until(lambda: len(frames) == 1)
        await client.stop()
