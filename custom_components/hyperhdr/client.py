"""Async WebSocket client for the HyperHDR JSON-RPC API.

Pure Python + aiohttp -- no Home Assistant imports live here. The caller
supplies an ``aiohttp.ClientSession`` (this module never creates its own);
TLS verification is a property of that session, not of this client.

Two concrete clients share ``HyperHdrBaseClient``'s connect/auth/reconnect/
watchdog machinery:

- ``HyperHdrServerClient``: sysinfo, instance roster, instance lifecycle.
- ``HyperHdrInstanceClient``: parked on one instance (via ``switchTo``),
  issues color/effect/adjustment/component commands for it.

Responses are matched to requests by ``tan`` ONLY -- the server blanks or
rewrites ``command`` on errors and subcommand acks (see docs/api-notes.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import aiohttp

from .const import (
    DEFAULT_HEARTBEAT,
    DEFAULT_ORIGIN,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_STALE_TIMEOUT,
    IMAGESTREAM_UPDATE_TOPIC,
    LEDSTREAM_UPDATE_TOPIC,
    RECONNECT_BASE_DELAY,
    RECONNECT_MAX_DELAY,
    SERVER_SUBSCRIPTIONS,
    SUBSCRIPTIONS,
    WATCHDOG_INTERVAL,
)
from .exceptions import HyperHdrApiError, HyperHdrAuthError, HyperHdrConnectionError, HyperHdrError
from .models import HyperHdrSysInfo

_LOGGER = logging.getLogger(__name__)

_CLOSING_TYPES = (
    aiohttp.WSMsgType.CLOSE,
    aiohttp.WSMsgType.CLOSING,
    aiohttp.WSMsgType.CLOSED,
    aiohttp.WSMsgType.ERROR,
)

OnConnectedCallback = Callable[[dict[str, Any]], "Awaitable[None] | None"]
OnDisconnectedCallback = Callable[[], Any]
OnAuthFailedCallback = Callable[[], Any]

# The exponent is clamped before ``2**attempt`` -- past this point the
# uncapped delay already vastly exceeds RECONNECT_MAX_DELAY (with the
# current defaults, that happens by attempt 5), so the clamp never changes
# the *result*, only guards the float conversion below it from overflowing
# for a very large `attempt`. Without it, a sufficiently long real-world
# outage (attempt increments roughly once per retry, i.e. every
# RECONNECT_MAX_DELAY-ish seconds once saturated -- reachable within a day
# or two of continuous failures) eventually raises OverflowError computing
# ``float * 2**attempt``, permanently killing the reconnect supervisor task
# with nothing left to resurrect it.
_MAX_BACKOFF_ATTEMPT = 20


def _backoff_delay(attempt: int) -> float:
    """The deterministic (pre-jitter) reconnect backoff delay for `attempt`."""
    return float(min(RECONNECT_BASE_DELAY * (2 ** min(attempt, _MAX_BACKOFF_ATTEMPT)), RECONNECT_MAX_DELAY))


class HyperHdrBaseClient:
    """Shared transport: connect, auth handshake, tan correlation, push
    dispatch, reconnect supervisor, staleness watchdog."""

    _subscriptions: tuple[str, ...] = ()

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        *,
        use_ssl: bool = False,
        token: str | None = None,
        admin_password: str | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        heartbeat: float = DEFAULT_HEARTBEAT,
        stale_timeout: float = DEFAULT_STALE_TIMEOUT,
        on_connected: OnConnectedCallback | None = None,
        on_disconnected: OnDisconnectedCallback | None = None,
        on_auth_failed: OnAuthFailedCallback | None = None,
    ) -> None:
        self._session = session
        self._host = host
        self._port = port
        self._use_ssl = use_ssl
        self._token = token
        self._admin_password = admin_password
        self._request_timeout = request_timeout
        self._heartbeat = heartbeat
        self._stale_timeout = stale_timeout

        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self.on_auth_failed = on_auth_failed

        self.admin_logged_in = False
        self.token_required = False
        self.malformed_or_unmatched_count = 0

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._tan_counter: Iterator[int] = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._push_callbacks: dict[str, Callable[[dict[str, Any]], None]] = {}
        self._last_rx: float = 0.0
        self._handshake_complete = False
        self._reached_connected = False
        self._stopping = False
        self._supervisor_task: asyncio.Task[None] | None = None

        # Injectable for tests -- default to the real event loop clock.
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        self._monotonic: Callable[[], float] = time.monotonic

    @property
    def ws_url(self) -> str:
        """The WebSocket URL to connect to (path is always ``/``)."""
        scheme = "wss" if self._use_ssl else "ws"
        return f"{scheme}://{self._host}:{self._port}/"

    @property
    def connected(self) -> bool:
        """Whether the socket is open AND the connect handshake completed."""
        return self._ws is not None and not self._ws.closed and self._handshake_complete

    async def start(self) -> None:
        """Start the reconnect supervisor (connect + reconnect loop)."""
        if self._supervisor_task is not None:
            return
        self._stopping = False
        self._supervisor_task = asyncio.ensure_future(self._supervisor_loop())

    async def stop(self) -> None:
        """Stop the supervisor, close the socket, fail any pending requests."""
        self._stopping = True
        task, self._supervisor_task = self._supervisor_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._fail_pending(HyperHdrConnectionError("client stopped"))

    def set_push_callback(self, topic: str, cb: Callable[[dict[str, Any]], None] | None) -> None:
        """Register (or, with ``cb=None``, unregister) a per-topic push handler."""
        if cb is None:
            self._push_callbacks.pop(topic, None)
        else:
            self._push_callbacks[topic] = cb

    # --- subclass hooks ------------------------------------------------

    async def _post_connect(self) -> None:
        """Hook run after auth, before the subscribing serverinfo call."""

    # --- reconnect supervisor -------------------------------------------

    async def _supervisor_loop(self) -> None:
        attempt = 0
        while not self._stopping:
            self._reached_connected = False
            try:
                await self._connect_once()
            except HyperHdrAuthError:
                await self._invoke(self.on_auth_failed)
                return
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - any connect/protocol failure retries
                # WARNING (not DEBUG): a persistent outage must be visible by default.
                _LOGGER.warning("connect attempt failed: %s", err)

            await self._invoke(self.on_disconnected)
            if self._reached_connected:
                attempt = 0
            if self._stopping:
                return
            delay = _backoff_delay(attempt) * random.uniform(0.8, 1.2)
            attempt += 1
            await self._sleep(delay)

    async def _connect_once(self) -> None:
        # heartbeat intentionally NOT forwarded to aiohttp's ws_connect --
        # confirmed live, Phase 5+6: this server's reply to aiohttp's
        # low-level PING is a malformed fragmented control frame, which
        # aiohttp correctly rejects per RFC 6455 by force-closing the
        # connection, reproducing every `heartbeat` seconds like clockwork
        # (root-caused with a raw aiohttp probe against hyperhdr-dev;
        # matches the "sent 1002 fragmented control frame" gotcha
        # docs/api-notes.md already noted for ledstream). `self._heartbeat`
        # is instead used by `_watchdog`'s app-level keepalive (Phase
        # 7+8) below -- see DEFAULT_HEARTBEAT's note in const.py.
        ws = await self._session.ws_connect(self.ws_url, heartbeat=None)
        self._ws = ws
        self._tan_counter = itertools.count(1)
        self._pending = {}
        self._last_rx = self._monotonic()
        self._handshake_complete = False
        self.admin_logged_in = False

        receive_task: asyncio.Task[None] = asyncio.ensure_future(self._receive_loop())
        watchdog_task: asyncio.Task[None] = asyncio.ensure_future(self._watchdog())
        try:
            await self._auth_handshake()
            await self._post_connect()
            response = await self._send_command("serverinfo", subscribe=list(self._subscriptions))
            info = response.get("info")
            self._handshake_complete = True
            self._reached_connected = True
            await self._invoke(self.on_connected, info if isinstance(info, dict) else {})
            await receive_task
        finally:
            self._handshake_complete = False
            watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog_task
            if not ws.closed:
                await ws.close()
            if not receive_task.done():
                receive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await receive_task
            self._fail_pending(HyperHdrConnectionError("connection closed"))
            self._ws = None

    async def _auth_handshake(self) -> None:
        self.admin_logged_in = False
        response = await self._send_command("authorize", subcommand="tokenRequired")
        info = response.get("info")
        # Set as a public attribute (not a local) so it survives even when
        # this method goes on to raise below -- the config flow's one-shot
        # validation reads it to distinguish "token required" from other
        # auth failures.
        self.token_required = bool(info.get("required", False)) if isinstance(info, dict) else False

        if self.token_required:
            if not self._token:
                raise HyperHdrAuthError("server requires a token but none is configured")
            login_response = await self._send_command(
                "authorize", subcommand="login", token=self._token, raise_on_error=False
            )
            if not login_response.get("success", False):
                raise HyperHdrAuthError(f"token login failed: {login_response.get('error', '')}")

        if self._admin_password:
            login_response = await self._send_command(
                "authorize", subcommand="login", password=self._admin_password, raise_on_error=False
            )
            if not login_response.get("success", False):
                raise HyperHdrAuthError(f"admin login failed: {login_response.get('error', '')}")
            self.admin_logged_in = True

    # --- request/response correlation -------------------------------------

    async def _send_command(
        self,
        command: str,
        *,
        subcommand: str | None = None,
        timeout: float | None = None,
        raise_on_error: bool = True,
        **payload: Any,
    ) -> dict[str, Any]:
        """Send a tan-correlated command and await its matching response.

        Raises ``HyperHdrConnectionError`` if not connected, on send
        failure, or on timeout. Raises ``HyperHdrApiError`` if the response
        carries ``success: false``, unless ``raise_on_error`` is False (used
        for auth probing, which needs the raw response dict even on failure).
        """
        if self._ws is None or self._ws.closed:
            raise HyperHdrConnectionError("not connected")

        tan = next(self._tan_counter)
        message: dict[str, Any] = {"command": command, "tan": tan}
        if subcommand is not None:
            message["subcommand"] = subcommand
        message.update(payload)

        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[tan] = future

        _LOGGER.debug("send: command=%s subcommand=%s tan=%s", command, subcommand, tan)
        try:
            await self._ws.send_json(message)
        except Exception as err:
            self._pending.pop(tan, None)
            raise HyperHdrConnectionError(f"failed to send {command} (tan={tan}): {err}") from err

        effective_timeout = timeout if timeout is not None else self._request_timeout
        try:
            response = await asyncio.wait_for(future, timeout=effective_timeout)
        except TimeoutError as err:
            self._pending.pop(tan, None)
            raise HyperHdrConnectionError(f"timed out waiting for response to {command} (tan={tan})") from err

        if raise_on_error and not response.get("success", True):
            raise HyperHdrApiError(command=str(response.get("command", command)), error=str(response.get("error", "")))
        return response

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        ws = self._ws
        try:
            while True:
                msg = await ws.receive()
                self._last_rx = self._monotonic()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._handle_text_frame(msg.data)
                    continue
                if msg.type in _CLOSING_TYPES:
                    return
        finally:
            self._fail_pending(HyperHdrConnectionError("connection closed"))

    def _handle_text_frame(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            self.malformed_or_unmatched_count += 1
            _LOGGER.debug("dropping malformed (non-JSON) frame")
            return
        if not isinstance(data, dict):
            self.malformed_or_unmatched_count += 1
            _LOGGER.debug("dropping malformed (non-object) frame")
            return
        self._route_message(data)

    def _route_message(self, data: dict[str, Any]) -> None:
        # (a) push topics never resolve a pending future -- ledstream frames
        # reuse the request's tan on every frame, which would otherwise
        # corrupt correlation.
        command = data.get("command")
        if isinstance(command, str) and command.endswith("-update"):
            self._dispatch_push(command, data)
            return

        # (b) tan-matched command response.
        tan = data.get("tan")
        future = self._pending.pop(tan, None) if isinstance(tan, int) else None
        if future is not None and not future.done():
            future.set_result(data)
            return

        # (c) unmatched/unsolicited -- drop and count.
        self.malformed_or_unmatched_count += 1
        _LOGGER.debug("dropping frame with unmatched tan (command=%s)", command)

    def _dispatch_push(self, topic: str, data: dict[str, Any]) -> None:
        callback = self._push_callbacks.get(topic)
        if callback is None:
            _LOGGER.debug("no push callback registered for topic %s", topic)
            return
        try:
            callback(data)
        except Exception:
            # A broken consumer must not be allowed to kill the receive loop.
            _LOGGER.exception("push callback for topic %s raised", topic)

    def _fail_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)

    # --- staleness watchdog + app-level keepalive -----------------------

    async def _watchdog(self) -> None:
        while True:
            await self._sleep(WATCHDOG_INTERVAL)
            if self._ws is None or self._ws.closed:
                return
            idle = self._monotonic() - self._last_rx
            if idle >= self._stale_timeout:
                _LOGGER.debug("connection stale (no rx for >= %.1fs), forcing close", self._stale_timeout)
                await self._ws.close()
                return
            if idle >= self._heartbeat:
                await self._send_keepalive()

    async def _send_keepalive(self) -> None:
        """Send a lightweight ``sysinfo`` request to refresh ``_last_rx``.

        Only reached once the connection has sat idle (no received frame,
        pushed or otherwise) for ``self._heartbeat`` seconds -- see
        DEFAULT_HEARTBEAT's note in const.py for why this exists (the
        low-level WS ping is disabled; the server otherwise sends nothing
        at all while idle, and the staleness watchdog above would
        eventually force-close a connection that's actually still healthy).

        The response is matched/consumed like any other command through the
        normal tan-correlated request path, and ``_receive_loop`` already
        stamps ``_last_rx`` on every received frame -- so a successful
        response alone is enough to reset the idle clock; nothing else
        needs to happen here on success.

        A failure (timeout, or the rare ``success: false``) is deliberately
        swallowed, not re-raised: this runs inside the watchdog's own loop,
        which must keep running regardless. Nothing bespoke is needed for
        the failure case either -- ``_last_rx`` stays stale, and the next
        watchdog tick's staleness check (above) will force a reconnect once
        ``stale_timeout`` is reached, the same as if no keepalive had been
        attempted at all.
        """
        try:
            await self._send_command("sysinfo", timeout=self._request_timeout)
        except HyperHdrError as err:
            _LOGGER.debug("keepalive request failed, deferring to the staleness check: %s", err)

    # --- misc -------------------------------------------------------------

    async def _invoke(self, cb: Callable[..., Any] | None, *args: Any) -> None:
        if cb is None:
            return
        result = cb(*args)
        if inspect.isawaitable(result):
            await result


class HyperHdrServerClient(HyperHdrBaseClient):
    """Server-scoped client: sysinfo, instance roster, instance lifecycle."""

    _subscriptions = SERVER_SUBSCRIPTIONS

    async def async_sysinfo(self) -> HyperHdrSysInfo:
        """Fetch and parse the server's ``sysinfo``."""
        response = await self._send_command("sysinfo")
        info = response.get("info")
        return HyperHdrSysInfo.from_dict(info if isinstance(info, dict) else {})

    async def async_connect_once(self) -> HyperHdrSysInfo:
        """Single connect + auth handshake + ``sysinfo`` fetch, then always
        disconnect -- no supervisor/retry loop, no push subscriptions, and
        no staleness watchdog (this is a short-lived, one-shot probe, not a
        persistent connection).

        Used by the config flow's connection validation, which needs one
        deterministic outcome per attempt rather than the supervisor's
        infinite retry loop. Raises ``HyperHdrAuthError`` on a bad/missing
        token or admin password (``self.token_required`` is set as a side
        effect of the attempt either way -- even when this raises -- so the
        caller can distinguish "token required" from other auth failures)
        or ``HyperHdrConnectionError`` on any transport failure.
        """
        # heartbeat intentionally NOT forwarded to aiohttp -- see
        # _connect_once's identical call for why (this probe is short-lived
        # enough that it wouldn't normally hit the heartbeat interval
        # anyway, but consistency avoids surprises if that ever changes).
        ws = await self._session.ws_connect(self.ws_url, heartbeat=None)
        self._ws = ws
        self._tan_counter = itertools.count(1)
        self._pending = {}
        self._last_rx = self._monotonic()
        self._handshake_complete = False
        self.admin_logged_in = False

        receive_task: asyncio.Task[None] = asyncio.ensure_future(self._receive_loop())
        try:
            await self._auth_handshake()
            self._handshake_complete = True
            return await self.async_sysinfo()
        finally:
            self._handshake_complete = False
            if not ws.closed:
                await ws.close()
            if not receive_task.done():
                receive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await receive_task
            self._fail_pending(HyperHdrConnectionError("connection closed"))
            self._ws = None

    async def start_instance(self, instance_id: int) -> None:
        """Start a configured instance.

        No admin login is required for ``startInstance`` on HyperHDR v22
        (verified live -- see docs/api-notes.md); the command is always
        sent, and a server-side rejection surfaces as ``HyperHdrApiError``
        via the existing ``success: false`` path.

        ``startInstance`` emits no push on its own (see docs/api-notes.md)
        -- a fresh ``serverinfo`` roster is fetched and fed to the
        ``instance-update`` push callback as a synthetic push.
        """
        await self._send_command("instance", subcommand="startInstance", instance=instance_id)
        response = await self._send_command("serverinfo", subscribe=list(self._subscriptions))
        info = response.get("info")
        roster = info.get("instance", []) if isinstance(info, dict) else []
        self._dispatch_push("instance-update", {"command": "instance-update", "data": roster})

    async def stop_instance(self, instance_id: int) -> None:
        """Stop a running instance.

        No admin login is required for ``stopInstance`` on HyperHDR v22
        (verified live -- see docs/api-notes.md); the command is always
        sent, and a server-side rejection surfaces as ``HyperHdrApiError``
        via the existing ``success: false`` path.
        """
        await self._send_command("instance", subcommand="stopInstance", instance=instance_id)

    async def create_instance(self, name: str) -> None:
        """Create a new (not-running) instance with friendly name ``name``.

        Reserved for future use: no HA-facing caller (no service, no config
        flow step) wires this up yet -- it's exercised directly by its own
        client-level tests only. Kept because the transport-layer support is
        cheap and correct today; deliberately not building a UI/service
        around it until there's an actual feature request for it.

        ``createInstance`` requires an admin login on HyperHDR v22 (verified
        live -- see docs/api-notes.md); the command is always sent, and a
        rejection (e.g. no admin login) surfaces as ``HyperHdrApiError`` via
        the existing ``success: false`` path -- this method does not
        preemptively check ``admin_logged_in``.

        A live re-probe found ``createInstance`` does not reliably push an
        ``instance-update`` on its own (contradicting the earlier docs/api-
        notes.md recon) -- like ``start_instance``, a fresh ``serverinfo``
        roster is fetched and fed to the ``instance-update`` push callback
        as a synthetic push.
        """
        await self._send_command("instance", subcommand="createInstance", name=name)
        response = await self._send_command("serverinfo", subscribe=list(self._subscriptions))
        info = response.get("info")
        roster = info.get("instance", []) if isinstance(info, dict) else []
        self._dispatch_push("instance-update", {"command": "instance-update", "data": roster})

    async def delete_instance(self, instance_id: int) -> None:
        """Delete an existing instance.

        Reserved for future use, same as ``create_instance`` above: no
        HA-facing caller wires this up yet (no service, no UI action) --
        intentional, not an oversight.

        Grouped with ``createInstance`` as admin-gated in docs/api-notes.md
        (not independently re-verified); sent unconditionally like every
        other instance-lifecycle command here, letting a rejection surface
        as ``HyperHdrApiError``. Synthesizes a roster-refresh push for the
        same reason as ``create_instance``/``start_instance``.
        """
        await self._send_command("instance", subcommand="deleteInstance", instance=instance_id)
        response = await self._send_command("serverinfo", subscribe=list(self._subscriptions))
        info = response.get("info")
        roster = info.get("instance", []) if isinstance(info, dict) else []
        self._dispatch_push("instance-update", {"command": "instance-update", "data": roster})


class HyperHdrInstanceClient(HyperHdrBaseClient):
    """A client parked on a single HyperHDR instance via ``switchTo``."""

    _subscriptions = SUBSCRIPTIONS

    _ADJUSTMENT_FIELD_MAP: dict[str, str] = {
        "luminance_gain": "luminanceGain",
        "saturation_gain": "saturationGain",
        "backlight_threshold": "backlightThreshold",
        "gamma": "gamma",
        "temperature_red": "temperatureRed",
        "temperature_green": "temperatureGreen",
        "temperature_blue": "temperatureBlue",
    }

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        *,
        instance_id: int,
        use_ssl: bool = False,
        token: str | None = None,
        admin_password: str | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        heartbeat: float = DEFAULT_HEARTBEAT,
        stale_timeout: float = DEFAULT_STALE_TIMEOUT,
        on_connected: OnConnectedCallback | None = None,
        on_disconnected: OnDisconnectedCallback | None = None,
        on_auth_failed: OnAuthFailedCallback | None = None,
    ) -> None:
        super().__init__(
            session,
            host,
            port,
            use_ssl=use_ssl,
            token=token,
            admin_password=admin_password,
            request_timeout=request_timeout,
            heartbeat=heartbeat,
            stale_timeout=stale_timeout,
            on_connected=on_connected,
            on_disconnected=on_disconnected,
            on_auth_failed=on_auth_failed,
        )
        self.instance_id = instance_id
        self._ledstream_refcount = 0
        self._imagestream_refcount = 0
        # Ledstream frames fan out to every registered consumer (unlike the
        # base class's single-slot ``_push_callbacks`` table) -- camera.py
        # can have concurrent consumers (a still capture requested while an
        # MJPEG stream is open). Kept separate from ``_push_callbacks`` rather
        # than generalizing that table: every other push topic (components/
        # priorities/adjustment/effects/instance roster) has exactly one
        # consumer (the coordinator), so fan-out there would be needless
        # complexity for a case that never happens. See ``_dispatch_push``'s
        # override below.
        self._ledstream_callbacks: list[Callable[[dict[str, Any]], None]] = []

    def _dispatch_push(self, topic: str, data: dict[str, Any]) -> None:
        """Fan ``LEDSTREAM_UPDATE_TOPIC`` frames out to every registered
        consumer; every other topic still goes through the inherited
        single-callback-per-topic table unchanged."""
        if topic != LEDSTREAM_UPDATE_TOPIC:
            super()._dispatch_push(topic, data)
            return
        if not self._ledstream_callbacks:
            _LOGGER.debug("no push callback registered for topic %s", topic)
            return
        for callback in list(self._ledstream_callbacks):
            try:
                callback(data)
            except Exception:
                # A broken consumer must not be allowed to kill the receive
                # loop, nor prevent the frame from reaching the other
                # registered consumers.
                _LOGGER.exception("push callback for topic %s raised", topic)

    async def _post_connect(self) -> None:
        """Park this connection on ``instance_id`` via ``instance/switchTo``.

        ``switchTo`` does not require an admin login on HyperHDR 22.0.0beta2
        (verified live) -- this only converts an unexpected auth failure
        into ``HyperHdrAuthError`` when no admin password was configured to
        attempt one.
        """
        try:
            await self._send_command("instance", subcommand="switchTo", instance=self.instance_id)
        except HyperHdrApiError as err:
            if err.error == "No Authorization" and self._admin_password is None:
                raise HyperHdrAuthError(f"switching to instance {self.instance_id} requires an admin login") from err
            raise

    async def async_set_color(self, rgb: tuple[int, int, int], priority: int, duration_ms: int = 0) -> None:
        """Set a solid color at ``priority``."""
        await self._send_command(
            "color", color=list(rgb), priority=priority, origin=DEFAULT_ORIGIN, duration=duration_ms
        )

    async def async_set_effect(self, name: str, priority: int, duration_ms: int = 0) -> None:
        """Start effect ``name`` at ``priority``."""
        await self._send_command(
            "effect", effect={"name": name}, priority=priority, origin=DEFAULT_ORIGIN, duration=duration_ms
        )

    async def async_clear(self, priority: int) -> None:
        """Clear ``priority`` (``-1`` clears all priorities)."""
        await self._send_command("clear", priority=priority)

    async def async_set_component(self, name: str, state: bool) -> None:
        """Enable/disable a component (e.g. ``SMOOTHING``, ``LEDDEVICE``)."""
        await self._send_command("componentstate", componentstate={"component": name, "state": state})

    async def async_set_adjustment(self, **fields: Any) -> None:
        """Send only the given adjustment fields (never the cached full object).

        Keys are python_name -> apiName mapped via ``_ADJUSTMENT_FIELD_MAP``.
        """
        payload: dict[str, Any] = {}
        for python_name, value in fields.items():
            api_name = self._ADJUSTMENT_FIELD_MAP.get(python_name)
            if api_name is None:
                raise ValueError(f"unknown adjustment field: {python_name}")
            payload[api_name] = value
        if not payload:
            return
        await self._send_command("adjustment", adjustment=payload)

    async def async_set_hdr_mode(self, mode: int) -> None:
        """Set HDR tone mapping mode via ``videomodehdr``."""
        await self._send_command("videomodehdr", HDR=mode)

    async def async_select_source(self, priority: int | None) -> None:
        """Select a fixed priority source, or ``None`` for auto-select."""
        if priority is None:
            await self._send_command("sourceselect", auto=True)
        else:
            await self._send_command("sourceselect", priority=priority)

    async def start_ledstream(self, cb: Callable[[dict[str, Any]], None]) -> None:
        """Register ``cb`` for LED-color stream frames, refcounted.

        Every registered ``cb`` receives every frame (see
        ``_dispatch_push``'s fan-out override) -- multiple concurrent
        consumers (e.g. a still capture requested while camera.py's MJPEG
        stream is open) each get their own copy of the stream rather than
        racing over a single slot.

        Rolls the refcount/callback back on a failed ``ledstream-start``
        send (e.g. the connection drops mid-call) -- otherwise a raised
        exception here would leave the refcount permanently incremented
        with no matching ``stop_ledstream`` ever having a chance to run
        (camera.py, this refcounted API's first real caller, calls
        ``start_ledstream`` outside its own try/finally, precisely so a
        failure here doesn't also swallow the original error), silently
        breaking every future ``start_ledstream`` on this connection (they'd
        see a non-zero refcount and skip resending the real start command).
        """
        self._ledstream_refcount += 1
        self._ledstream_callbacks.append(cb)
        if self._ledstream_refcount == 1:
            try:
                await self._send_command("ledcolors", subcommand="ledstream-start")
            except Exception:
                self._ledstream_refcount -= 1
                self._ledstream_callbacks.remove(cb)
                raise

    async def stop_ledstream(self, cb: Callable[[dict[str, Any]], None]) -> None:
        """Decrement the ledstream refcount and unregister ``cb``, stopping
        the stream (for every remaining consumer too) only once the last
        one has stopped."""
        if self._ledstream_refcount == 0:
            return
        self._ledstream_refcount -= 1
        with contextlib.suppress(ValueError):
            self._ledstream_callbacks.remove(cb)
        if self._ledstream_refcount == 0:
            await self._send_command("ledcolors", subcommand="ledstream-stop")

    async def start_imagestream(self, cb: Callable[[dict[str, Any]], None]) -> None:
        """Register ``cb`` for image-preview stream frames, refcounted.

        Unlike ledstream, imagestream requires an admin login (verified live).
        """
        if not self.admin_logged_in:
            raise HyperHdrAuthError("imagestream requires an admin login")
        self._imagestream_refcount += 1
        self.set_push_callback(IMAGESTREAM_UPDATE_TOPIC, cb)
        if self._imagestream_refcount == 1:
            try:
                await self._send_command("ledcolors", subcommand="imagestream-start")
            except Exception:
                self._imagestream_refcount -= 1
                self.set_push_callback(IMAGESTREAM_UPDATE_TOPIC, None)
                raise

    async def stop_imagestream(self, cb: Callable[[dict[str, Any]], None]) -> None:
        """Decrement the imagestream refcount, stopping the stream at zero."""
        if self._imagestream_refcount == 0:
            return
        self._imagestream_refcount -= 1
        if self._imagestream_refcount == 0:
            self.set_push_callback(IMAGESTREAM_UPDATE_TOPIC, None)
            await self._send_command("ledcolors", subcommand="imagestream-stop")
