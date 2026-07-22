"""Shared test infrastructure: a scripted fake aiohttp WebSocket + session.

Deliberately lean -- only the aiohttp surface the client actually touches
(``ws_connect``, ``receive``/async iteration, ``send_json``, ``close``,
``closed``) is implemented.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

# Sentinel script item: closes the fake connection when reached.
DISCONNECT = object()


class FakeWebSocket:
    """A scripted stand-in for ``aiohttp.ClientWebSocketResponse``.

    Backed by a queue rather than a plain list so frames can be ``push()``ed
    live (after the initial script is exhausted) without racing a consumer
    that's already blocked waiting for the next item.

    Script items:
    - a ``dict``: delivered as a TEXT frame (JSON-encoded).
    - a ``str``: delivered as a raw (possibly malformed) TEXT frame verbatim.
    - a ``float``/``int``: an ``asyncio.sleep`` yield point before continuing.
    - ``DISCONNECT``: closes the connection (receive()/iteration ends).

    Every ``receive()`` call also does a bare ``asyncio.sleep(0)`` yield so
    that a task resuming from a just-resolved pending future (e.g. the next
    step of an auth handshake) gets scheduled *before* this fake races ahead
    to hand out the next scripted frame -- without it, two connected
    request/response steps can be delivered out of registration order.
    """

    def __init__(self, script: Iterable[Any] = ()) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        for item in script:
            self._queue.put_nowait(item)
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self.close_calls = 0

    def push(self, item: Any) -> None:
        """Enqueue another scripted item, e.g. to inject a live push frame."""
        self._queue.put_nowait(item)

    async def send_json(self, data: dict[str, Any]) -> None:
        if self.closed:
            raise ConnectionResetError("cannot send on a closed fake websocket")
        self.sent.append(data)

    async def receive(self) -> aiohttp.WSMessage:
        await asyncio.sleep(0)
        if self.closed:
            return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None)
        while True:
            item = await self._queue.get()
            if isinstance(item, (int, float)):
                await asyncio.sleep(item)
                continue
            if item is DISCONNECT:
                self.closed = True
                return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None)
            if isinstance(item, dict):
                return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(item), None)
            if isinstance(item, str):
                return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, item, None)
            raise TypeError(f"unsupported script item: {item!r}")

    def __aiter__(self) -> FakeWebSocket:
        return self

    async def __anext__(self) -> aiohttp.WSMessage:
        msg = await self.receive()
        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
            raise StopAsyncIteration
        return msg

    async def close(self) -> None:
        self.close_calls += 1
        if not self.closed:
            self.closed = True
            self._queue.put_nowait(DISCONNECT)


class FakeClientSession:
    """A fake ``aiohttp.ClientSession`` exposing only ``ws_connect()``.

    Hands out scripted ``FakeWebSocket`` instances in order -- one per
    connection attempt -- so reconnect tests can script a distinct frame
    sequence for each attempt.
    """

    def __init__(self, sockets: FakeWebSocket | Exception | Iterable[FakeWebSocket | Exception]) -> None:
        self._sockets: list[FakeWebSocket | Exception] = (
            [sockets] if isinstance(sockets, FakeWebSocket | Exception) else list(sockets)
        )
        self.ws_connect_calls: list[dict[str, Any]] = []

    async def ws_connect(self, url: str, **kwargs: Any) -> FakeWebSocket:
        self.ws_connect_calls.append({"url": url, **kwargs})
        if not self._sockets:
            raise ConnectionRefusedError("fake session has no more scripted sockets")
        item = self._sockets.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    """Poll ``predicate`` (via cooperative sleep(0) yields) until it's true.

    Bounded by a small *real* timeout as a safety net against a genuinely
    hung test -- resolution in practice is near-instant since nothing here
    depends on wall-clock delays.
    """

    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=timeout)


# --- response-frame builders -------------------------------------------------
#
# Small helpers for assembling scripted response sequences that match what
# HyperHdrBaseClient actually sends during its connect handshake, so tests
# don't have to hand-compute tan numbers.


def token_required_frame(tan: int, required: bool) -> dict[str, Any]:
    return {"command": "authorize-tokenRequired", "success": True, "tan": tan, "info": {"required": required}}


def login_success_frame(tan: int, token: str = "fake-token") -> dict[str, Any]:
    return {"command": "authorize-login", "success": True, "tan": tan, "info": {"token": token}}


def login_failure_frame(tan: int, error: str = "No Authorization") -> dict[str, Any]:
    return {"command": "authorize", "success": False, "tan": tan, "error": error}


def switch_to_frame(tan: int, instance: int, success: bool = True, error: str = "No Authorization") -> dict[str, Any]:
    if success:
        return {"command": "instance-switchTo", "success": True, "tan": tan, "info": {"instance": instance}}
    return {"command": "instance", "success": False, "tan": tan, "error": error}


def serverinfo_frame(tan: int, info: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"command": "serverinfo", "success": True, "tan": tan, "info": info if info is not None else {}}


def build_connect_script(
    *,
    token_required: bool = False,
    token: str | None = None,
    token_login_success: bool = True,
    admin_password: str | None = None,
    admin_login_success: bool = True,
    is_instance: bool = False,
    instance_id: int = 0,
    switch_to_success: bool = True,
    serverinfo_info: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build the exact response sequence for a full connect handshake.

    Mirrors HyperHdrBaseClient's send order: authorize/tokenRequired ->
    [authorize/login token] -> [authorize/login password] -> [instance/
    switchTo] -> serverinfo. Stops early wherever the real client would
    (e.g. a required-but-absent token means no further requests are sent).
    """
    frames: list[dict[str, Any]] = []
    tan = 1
    frames.append(token_required_frame(tan, token_required))
    tan += 1
    if token_required:
        if token is None:
            return frames
        frames.append(login_success_frame(tan) if token_login_success else login_failure_frame(tan))
        tan += 1
        if not token_login_success:
            return frames
    if admin_password:
        frames.append(login_success_frame(tan) if admin_login_success else login_failure_frame(tan))
        tan += 1
        if not admin_login_success:
            return frames
    if is_instance:
        frames.append(switch_to_frame(tan, instance_id, switch_to_success))
        tan += 1
        if not switch_to_success:
            return frames
    frames.append(serverinfo_frame(tan, serverinfo_info))
    return frames
