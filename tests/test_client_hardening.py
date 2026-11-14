"""Transport hardening: handshake-gated commands, pending cleanup, stream
refcount races/reconnect, imagestream fan-out, backoff reset semantics."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import FakeClientSession, FakeWebSocket, build_connect_script, wait_until

from custom_components.hyperhdr.client import HyperHdrInstanceClient
from custom_components.hyperhdr.const import IMAGESTREAM_UPDATE_TOPIC, WATCHDOG_INTERVAL
from custom_components.hyperhdr.exceptions import HyperHdrConnectionError


def _bare_client() -> HyperHdrInstanceClient:
    return HyperHdrInstanceClient(object(), "localhost", 8090, instance_id=0, request_timeout=0.2)  # type: ignore[arg-type]


@pytest.fixture
async def connected_instance() -> Any:
    ws = FakeWebSocket(build_connect_script(is_instance=True, instance_id=0))
    client = HyperHdrInstanceClient(
        FakeClientSession(ws),  # type: ignore[arg-type]
        "localhost",
        8090,
        instance_id=0,
        request_timeout=0.2,
    )
    await client.start()
    await wait_until(lambda: client.connected)
    yield client, ws
    await client.stop()


class TestCommandsRequireHandshake:
    async def test_not_connected_raises(self) -> None:
        with pytest.raises(HyperHdrConnectionError):
            await _bare_client().async_clear(1)

    async def test_open_socket_without_handshake_raises(self) -> None:
        client = _bare_client()
        client._ws = FakeWebSocket([])  # type: ignore[assignment]
        with pytest.raises(HyperHdrConnectionError):
            await client.async_clear(1)


async def test_cancelled_request_does_not_leak_pending(connected_instance: Any) -> None:
    client, _ws = connected_instance
    task = asyncio.ensure_future(client._send_command("sysinfo"))
    await wait_until(lambda: len(client._pending) == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client._pending == {}


class TestLedstreamConcurrency:
    async def test_failed_first_start_lets_concurrent_waiter_send_its_own(self) -> None:
        client = _bare_client()
        gate = asyncio.Event()
        calls: list[str] = []

        async def _send(command: str, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs["subcommand"])
            if len(calls) == 1:
                await gate.wait()
                raise HyperHdrConnectionError("boom")
            return {}

        client._send_command = _send  # type: ignore[method-assign]
        cb_a, cb_b = (lambda d: None), (lambda d: None)
        first = asyncio.ensure_future(client.start_ledstream(cb_a))
        await wait_until(lambda: len(calls) == 1)
        second = asyncio.ensure_future(client.start_ledstream(cb_b))
        await asyncio.sleep(0)
        gate.set()

        with pytest.raises(HyperHdrConnectionError):
            await first
        await second
        assert calls == ["ledstream-start", "ledstream-start"]
        assert client._ledstream_refcount == 1
        assert client._ledstream_callbacks == [cb_b]

    async def test_resends_start_after_reconnect_handshake(self) -> None:
        client = _bare_client()
        sent: list[str] = []

        async def _send(command: str, **kwargs: Any) -> dict[str, Any]:
            sent.append(kwargs["subcommand"])
            return {}

        client._send_command = _send  # type: ignore[method-assign]
        await client._post_handshake()
        assert sent == []
        client._ledstream_refcount = 1
        await client._post_handshake()
        assert sent == ["ledstream-start"]


class TestImagestreamFanOut:
    async def test_every_consumer_gets_frames_and_stop_removes_only_its_cb(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _bare_client()
        client.admin_logged_in = True
        monkeypatch.setattr(HyperHdrInstanceClient, "connected", property(lambda self: True))
        sent: list[str] = []

        async def _send(command: str, **kwargs: Any) -> dict[str, Any]:
            sent.append(kwargs["subcommand"])
            return {}

        client._send_command = _send  # type: ignore[method-assign]
        a: list[dict[str, Any]] = []
        b: list[dict[str, Any]] = []
        await client.start_imagestream(a.append)
        await client.start_imagestream(b.append)
        assert sent == ["imagestream-start"]

        client._dispatch_push(IMAGESTREAM_UPDATE_TOPIC, {"n": 1})
        assert a == b == [{"n": 1}]

        await client.stop_imagestream(a.append)
        client._dispatch_push(IMAGESTREAM_UPDATE_TOPIC, {"n": 2})
        assert a == [{"n": 1}]
        assert b == [{"n": 1}, {"n": 2}]
        assert sent == ["imagestream-start"]

        await client.stop_imagestream(b.append)
        assert sent == ["imagestream-start", "imagestream-stop"]


async def test_failing_on_connected_does_not_reset_backoff() -> None:
    sockets = [FakeWebSocket(build_connect_script(is_instance=True, instance_id=0)) for _ in range(6)]
    client = HyperHdrInstanceClient(
        FakeClientSession(sockets),  # type: ignore[arg-type]
        "localhost",
        8090,
        instance_id=0,
        request_timeout=0.2,
        on_connected=lambda info: (_ for _ in ()).throw(ValueError("nope")),
    )
    delays: list[float] = []

    async def _sleep(delay: float) -> None:
        if delay == WATCHDOG_INTERVAL:
            await asyncio.sleep(3600)
        delays.append(delay)
        await asyncio.sleep(0)

    client._sleep = _sleep  # type: ignore[assignment]
    await client.start()
    await wait_until(lambda: len(delays) >= 3, timeout=3.0)
    await client.stop()
    assert delays[2] > delays[0] * 1.5


async def test_post_handshake_resend_failure_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_components.hyperhdr.exceptions import HyperHdrApiError

    client = _bare_client()
    client._ledstream_refcount = 1
    client._imagestream_refcount = 1
    client.admin_logged_in = True
    sent: list[str] = []

    async def _send(command: str, **kwargs: Any) -> dict[str, Any]:
        sent.append(kwargs["subcommand"])
        raise HyperHdrApiError(command=command, error="nope")

    client._send_command = _send  # type: ignore[method-assign]
    await client._post_handshake()
    assert sent == ["ledstream-start", "imagestream-start"]
