"""Shared test infrastructure: a scripted fake aiohttp WebSocket + session,
plus (from Phase 3 on) house-style ``homeassistant`` stubs.

Deliberately lean -- only the aiohttp surface the client actually touches
(``ws_connect``, ``receive``/async iteration, ``send_json``, ``close``,
``closed``) is implemented. The ``homeassistant`` stubs below are injected
into ``sys.modules`` at import time so ``coordinator.py``/``entity.py``/
``__init__.py`` can be imported without the real package installed; they are
pure additions that never touch ``aiohttp`` or any Phase 2 module, so the
existing client/model tests keep resolving to the real ``client.py``/
``models.py`` untouched.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
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


# --- fixture loading -----------------------------------------------------
#
# Real wire captures from a live HyperHDR 22.0.0beta2 server (see
# docs/api-notes.md). Tests load these verbatim rather than hand-typing
# response shapes, so a hand-typed frame can never silently drift from what
# the real server actually sends.

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """Load a verbatim wire-capture fixture from disk."""
    return json.loads((FIXTURES / name).read_text())


# --- response-frame builders -------------------------------------------------
#
# Small helpers for assembling scripted response sequences that match what
# HyperHdrBaseClient actually sends during its connect handshake, so tests
# don't have to hand-compute tan numbers. Where a real fixture exists for a
# frame shape, these load it from disk and patch only what scripting
# requires (tan correlation, or a variant like `required`).


def token_required_frame(tan: int, required: bool) -> dict[str, Any]:
    frame = load_fixture("authorize_token_required.json")
    frame["tan"] = tan
    frame["info"]["required"] = required
    return frame


def ledstream_update_frame(tan: int) -> dict[str, Any]:
    frame = load_fixture("ledcolors_ledstream_update.json")
    frame["tan"] = tan
    return frame


def videomodehdr_response_frame(tan: int) -> dict[str, Any]:
    frame = load_fixture("videomodehdr_response.json")
    frame["tan"] = tan
    return frame


def error_response_frame(tan: int) -> dict[str, Any]:
    """The real ``error_response.json`` capture: an unrecognized command
    gets back ``"command": ""`` (blanked, not echoed) -- only `tan` is
    patched here, preserving that trait for callers to assert against."""
    frame = load_fixture("error_response.json")
    frame["tan"] = tan
    return frame


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


def sysinfo_frame(tan: int, info: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"command": "sysinfo", "success": True, "tan": tan, "info": info if info is not None else {}}


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


# --- homeassistant stubs (Phase 3+) -----------------------------------------
#
# ``coordinator.py``/``entity.py``/``__init__.py`` are the first modules in
# this repo to import ``homeassistant``. Rather than pull in the real
# (heavyweight) package, inject a lean sys.modules stub -- modeled on the
# ha-awtrix house pattern -- covering only what those three files import.
# Real behavioral fidelity (DataUpdateCoordinator listener plumbing,
# CoordinatorEntity.available, entity/device registry purge calls, the
# dispatcher) is reproduced closely enough that unit tests can assert on it;
# anything not exercised by these files (e.g. the real config-flow surface)
# is intentionally left out.


def _make_module(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


class FakeConfigEntry:
    """Stand-in for ``homeassistant.config_entries.ConfigEntry``.

    ``runtime_data`` is deliberately never assigned in ``__init__`` --
    exactly like the real class, accessing it before ``async_setup_entry``
    assigns it raises ``AttributeError``, which the production code relies
    on (via ``hasattr``) to tell an in-progress first connect from a
    reconnect after setup already completed.
    """

    def __init__(
        self,
        *,
        entry_id: str = "test-entry",
        unique_id: str | None = None,
        title: str = "HyperHDR",
        data: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
    ) -> None:
        self.entry_id = entry_id
        self.unique_id = unique_id
        self.title = title
        self.data = data or {}
        self.options = options or {}
        self.update_listeners: list[Callable[..., Any]] = []
        self.on_unload_callbacks: list[Callable[[], Any]] = []
        self.reauth_started = False

    def async_on_unload(self, func: Callable[[], Any]) -> None:
        self.on_unload_callbacks.append(func)

    def add_update_listener(self, listener: Callable[..., Any]) -> Callable[[], None]:
        self.update_listeners.append(listener)
        return lambda: self.update_listeners.remove(listener)

    def async_start_reauth(self, hass: Any) -> None:
        self.reauth_started = True

    def __class_getitem__(cls, item: Any) -> Any:
        # Supports ``ConfigEntry[HyperHdrRuntimeData]`` (PEP 695 ``type``
        # alias RHS) without needing real Generic machinery.
        return cls


class FakeEntityEntry:
    """Stand-in for ``homeassistant.helpers.entity_registry.RegistryEntry``."""

    def __init__(self, entity_id: str, unique_id: str, config_entry_id: str) -> None:
        self.entity_id = entity_id
        self.unique_id = unique_id
        self.config_entry_id = config_entry_id


class FakeEntityRegistry:
    def __init__(self) -> None:
        self.entities: dict[str, FakeEntityEntry] = {}
        self.removed: list[str] = []

    def add(self, entity_id: str, unique_id: str, config_entry_id: str) -> FakeEntityEntry:
        entry = FakeEntityEntry(entity_id, unique_id, config_entry_id)
        self.entities[entity_id] = entry
        return entry


class FakeDeviceEntry:
    """Stand-in for ``homeassistant.helpers.device_registry.DeviceEntry``."""

    def __init__(self, device_id: str, identifiers: set[tuple[str, str]]) -> None:
        self.id = device_id
        self.identifiers = identifiers


class FakeDeviceRegistry:
    def __init__(self) -> None:
        self.devices: dict[str, FakeDeviceEntry] = {}
        self.removed: list[str] = []

    def add(self, device_id: str, identifiers: set[tuple[str, str]]) -> FakeDeviceEntry:
        entry = FakeDeviceEntry(device_id, identifiers)
        self.devices[device_id] = entry
        return entry

    def async_get_device(self, identifiers: set[tuple[str, str]]) -> FakeDeviceEntry | None:
        for entry in self.devices.values():
            if entry.identifiers & identifiers:
                return entry
        return None

    def async_remove_device(self, device_id: str) -> None:
        self.removed.append(device_id)
        self.devices.pop(device_id, None)


class _FakeConfigEntriesManager:
    """Stand-in for ``hass.config_entries``."""

    def __init__(self) -> None:
        self.forward_calls: list[tuple[Any, list[Any]]] = []
        self.unload_calls: list[tuple[Any, list[Any]]] = []
        self.reload_calls: list[str] = []

    async def async_forward_entry_setups(self, entry: Any, platforms: Iterable[Any]) -> None:
        self.forward_calls.append((entry, list(platforms)))

    async def async_unload_platforms(self, entry: Any, platforms: Iterable[Any]) -> bool:
        self.unload_calls.append((entry, list(platforms)))
        return True

    async def async_reload(self, entry_id: str) -> None:
        self.reload_calls.append(entry_id)


class FakeHass:
    """Stand-in for ``homeassistant.core.HomeAssistant``.

    ``async_create_task`` schedules a real ``asyncio.Task`` (so pushed
    coroutines -- e.g. the diff handler triggered by an ``instance-update``
    push -- actually run); ``async_block_till_done`` (mirroring the real
    test helper of the same name) drains them so a test can deterministically
    wait for a fire-and-forget task without a real sleep.
    """

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.config_entries = _FakeConfigEntriesManager()
        self.entity_registry = FakeEntityRegistry()
        self.device_registry = FakeDeviceRegistry()
        self.client_session = object()
        self.insecure_client_session = object()
        self.dispatcher_calls: dict[str, list[tuple[Any, ...]]] = {}
        self.dispatcher_listeners: dict[str, list[Callable[..., Any]]] = {}
        self._tasks: list[asyncio.Task[Any]] = []

    def async_create_task(self, coro: Any, name: str | None = None, eager_start: bool = False) -> asyncio.Task[Any]:
        task: asyncio.Task[Any] = asyncio.ensure_future(coro)
        self._tasks.append(task)
        return task

    async def async_block_till_done(self) -> None:
        while self._tasks:
            tasks, self._tasks = self._tasks, []
            await asyncio.gather(*tasks)


class FakeInstanceClient:
    """A lightweight double for ``HyperHdrInstanceClient``: records
    start()/stop() calls and exposes the on_connected/on_disconnected/
    on_auth_failed slots + push callback registry a coordinator's
    ``attach_client`` wires up, without any real networking."""

    def __init__(self, instance_id: int = 0) -> None:
        self.instance_id = instance_id
        self.start_calls = 0
        self.stop_calls = 0
        self.on_connected: Callable[..., Any] | None = None
        self.on_disconnected: Callable[..., Any] | None = None
        self.on_auth_failed: Callable[..., Any] | None = None
        self.push_callbacks: dict[str, Callable[[dict[str, Any]], None]] = {}

    def set_push_callback(self, topic: str, cb: Callable[[dict[str, Any]], None] | None) -> None:
        if cb is None:
            self.push_callbacks.pop(topic, None)
        else:
            self.push_callbacks[topic] = cb

    async def start(self) -> None:
        self.start_calls += 1

    async def stop(self) -> None:
        self.stop_calls += 1


class FakeServerClient:
    """A double for ``HyperHdrServerClient`` for entry-setup tests.

    ``start()`` synchronously drives the same callback slots the real
    client would invoke (``on_connected``/``on_auth_failed``), configurable
    per test via ``connect_behavior``/``connect_info``/``sysinfo_result`` so
    a test can script a clean connect, a timeout (never resolves), or an
    auth failure without any real socket or real sleep.
    """

    def __init__(self, session: Any, host: str, port: int, **kwargs: Any) -> None:
        self.session = session
        self.host = host
        self.port = port
        self.kwargs = kwargs
        self.on_connected = kwargs.get("on_connected")
        self.on_disconnected = kwargs.get("on_disconnected")
        self.on_auth_failed = kwargs.get("on_auth_failed")
        self.push_callbacks: dict[str, Callable[[dict[str, Any]], None]] = {}
        self.start_calls = 0
        self.stop_calls = 0
        # Test-controlled behavior:
        self.connect_behavior = "success"  # "success" | "timeout" | "auth_failed"
        self.connect_info: dict[str, Any] = {"instance": []}
        self.sysinfo_result: Any = None

    def set_push_callback(self, topic: str, cb: Callable[[dict[str, Any]], None] | None) -> None:
        if cb is None:
            self.push_callbacks.pop(topic, None)
        else:
            self.push_callbacks[topic] = cb

    async def start(self) -> None:
        self.start_calls += 1
        if self.connect_behavior == "success" and self.on_connected is not None:
            await self.on_connected(self.connect_info)
        elif self.connect_behavior == "auth_failed" and self.on_auth_failed is not None:
            self.on_auth_failed()
        # "timeout": deliberately calls neither callback.

    async def stop(self) -> None:
        self.stop_calls += 1

    async def async_sysinfo(self) -> Any:
        return self.sysinfo_result


def _stub_homeassistant() -> None:
    """Inject minimal ``homeassistant`` stubs so coordinator/entity/__init__
    can be imported without the real package."""
    if "homeassistant" in sys.modules and getattr(sys.modules["homeassistant"], "_hyperhdr_stub", False):
        return

    ha = _make_module("homeassistant")
    ha._hyperhdr_stub = True

    ha_core = _make_module(
        "homeassistant.core",
        HomeAssistant=FakeHass,
        callback=lambda func: func,
    )
    ha.core = ha_core

    ha_ce = _make_module("homeassistant.config_entries", ConfigEntry=FakeConfigEntry)
    ha.config_entries = ha_ce

    class _Platform:
        pass

    ha_const = _make_module("homeassistant.const", Platform=_Platform, CONF_HOST="host", CONF_PORT="port")
    ha.const = ha_const

    class ConfigEntryNotReady(Exception):
        pass

    class ConfigEntryAuthFailed(Exception):
        pass

    ha_exc = _make_module(
        "homeassistant.exceptions",
        ConfigEntryNotReady=ConfigEntryNotReady,
        ConfigEntryAuthFailed=ConfigEntryAuthFailed,
        HomeAssistantError=Exception,
    )
    ha.exceptions = ha_exc

    ha_helpers = _make_module("homeassistant.helpers")
    ha.helpers = ha_helpers

    from typing import Generic, TypeVar

    _T = TypeVar("_T")

    class _DataUpdateCoordinator(Generic[_T]):
        """Minimal stub reproducing the bits of DataUpdateCoordinator our
        code relies on: ``data``/``last_update_success``, listener
        add/remove, and ``async_set_updated_data`` notifying listeners.
        Deliberately has no ``_async_update_data``/refresh machinery --
        our coordinators never call it (``update_interval=None``)."""

        def __init__(
            self,
            hass: Any = None,
            logger: Any = None,
            *,
            name: str = "",
            update_interval: Any = None,
            config_entry: Any = None,
            **kwargs: Any,
        ) -> None:
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.config_entry = config_entry
            self.data: Any = None
            self.last_update_success = True
            self._listeners: list[Callable[[], None]] = []

        def async_set_updated_data(self, data: Any) -> None:
            self.data = data
            self.last_update_success = True
            for listener in list(self._listeners):
                listener()

        def async_add_listener(self, update_callback: Callable[[], None], context: Any = None) -> Callable[[], None]:
            self._listeners.append(update_callback)

            def _remove() -> None:
                if update_callback in self._listeners:
                    self._listeners.remove(update_callback)

            return _remove

    class _CoordinatorEntity(Generic[_T]):
        """Minimal stub for CoordinatorEntity: ``available`` reflects
        ``coordinator.last_update_success``, matching the real base class."""

        def __init__(self, coordinator: Any = None, **kwargs: Any) -> None:
            self.coordinator = coordinator

        @property
        def available(self) -> bool:
            return bool(self.coordinator.last_update_success)

    ha_uc = _make_module(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=_DataUpdateCoordinator,
        CoordinatorEntity=_CoordinatorEntity,
        UpdateFailed=Exception,
    )
    ha_helpers.update_coordinator = ha_uc

    ha_dr = _make_module(
        "homeassistant.helpers.device_registry",
        DeviceInfo=dict,
        async_get=lambda hass: hass.device_registry,
    )
    ha_helpers.device_registry = ha_dr

    def _er_async_entries_for_config_entry(registry: FakeEntityRegistry, config_entry_id: str) -> list[FakeEntityEntry]:
        return [e for e in registry.entities.values() if e.config_entry_id == config_entry_id]

    def _er_async_remove(registry: FakeEntityRegistry, entity_id: str) -> None:
        registry.removed.append(entity_id)
        registry.entities.pop(entity_id, None)

    ha_er = _make_module(
        "homeassistant.helpers.entity_registry",
        async_get=lambda hass: hass.entity_registry,
        async_entries_for_config_entry=_er_async_entries_for_config_entry,
        async_remove=_er_async_remove,
    )
    ha_helpers.entity_registry = ha_er

    def _async_dispatcher_send(hass: Any, signal: str, *args: Any) -> None:
        hass.dispatcher_calls.setdefault(signal, []).append(args)
        for target in list(hass.dispatcher_listeners.get(signal, [])):
            target(*args)

    def _async_dispatcher_connect(hass: Any, signal: str, target: Callable[..., Any]) -> Callable[[], None]:
        hass.dispatcher_listeners.setdefault(signal, []).append(target)

        def _unsub() -> None:
            if target in hass.dispatcher_listeners.get(signal, []):
                hass.dispatcher_listeners[signal].remove(target)

        return _unsub

    ha_dispatcher = _make_module(
        "homeassistant.helpers.dispatcher",
        async_dispatcher_send=_async_dispatcher_send,
        async_dispatcher_connect=_async_dispatcher_connect,
    )
    ha_helpers.dispatcher = ha_dispatcher

    def _async_get_clientsession(hass: Any, verify_ssl: bool = True, **kwargs: Any) -> Any:
        return hass.client_session if verify_ssl else hass.insecure_client_session

    ha_ac = _make_module(
        "homeassistant.helpers.aiohttp_client",
        async_get_clientsession=_async_get_clientsession,
    )
    ha_helpers.aiohttp_client = ha_ac

    # Selector stubs for flow_support.py: real HA selectors are data-shape
    # wrappers around a config dict with no flow-manager behavior of their
    # own, so a bare "store what I was given" stand-in is faithful enough
    # for schema-shape tests (which only ever inspect vol.Schema's keys/
    # defaults, never call the selector as a validator).
    class _FakeSelector:
        def __init__(self, config: Any = None) -> None:
            self.config = config

        def __call__(self, data: Any) -> Any:
            # Real Selectors validate/coerce; voluptuous requires the
            # schema value to be callable to compile at all. Identity is
            # enough -- these tests only ever inspect vol.Schema's keys/
            # defaults, never feed data through the schema.
            return data

    class TextSelector(_FakeSelector):
        pass

    class NumberSelector(_FakeSelector):
        pass

    class BooleanSelector(_FakeSelector):
        pass

    class SelectSelector(_FakeSelector):
        pass

    def _selector_config(**kwargs: Any) -> dict[str, Any]:
        return dict(kwargs)

    class TextSelectorType:
        TEXT = "text"
        PASSWORD = "password"

    class NumberSelectorMode:
        BOX = "box"
        SLIDER = "slider"

    class SelectSelectorMode:
        DROPDOWN = "dropdown"
        LIST = "list"

    ha_selector = _make_module(
        "homeassistant.helpers.selector",
        TextSelector=TextSelector,
        TextSelectorConfig=_selector_config,
        TextSelectorType=TextSelectorType,
        NumberSelector=NumberSelector,
        NumberSelectorConfig=_selector_config,
        NumberSelectorMode=NumberSelectorMode,
        BooleanSelector=BooleanSelector,
        SelectSelector=SelectSelector,
        SelectSelectorConfig=_selector_config,
        SelectSelectorMode=SelectSelectorMode,
    )
    ha_helpers.selector = ha_selector

    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.core"] = ha_core
    sys.modules["homeassistant.config_entries"] = ha_ce
    sys.modules["homeassistant.const"] = ha_const
    sys.modules["homeassistant.exceptions"] = ha_exc
    sys.modules["homeassistant.helpers"] = ha_helpers
    sys.modules["homeassistant.helpers.update_coordinator"] = ha_uc
    sys.modules["homeassistant.helpers.device_registry"] = ha_dr
    sys.modules["homeassistant.helpers.entity_registry"] = ha_er
    sys.modules["homeassistant.helpers.dispatcher"] = ha_dispatcher
    sys.modules["homeassistant.helpers.aiohttp_client"] = ha_ac
    sys.modules["homeassistant.helpers.selector"] = ha_selector


_stub_homeassistant()
