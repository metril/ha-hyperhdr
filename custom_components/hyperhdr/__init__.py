"""The HyperHDR integration.

Entry setup wires the Phase 2 client to the coordinators from
coordinator.py, then hands off to the dynamic instance lifecycle: a
persistent ``HyperHdrInstanceCoordinator`` per instance, created the first
time that instance is observed running and kept alive (client detached, not
torn down) across stop/start cycles until the instance is deleted from the
server roster. See coordinator.py's module docstring for the full model.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .client import HyperHdrInstanceClient, HyperHdrServerClient
from .const import (
    CONF_ADMIN_PASSWORD,
    CONF_TOKEN,
    CONF_USE_SSL,
    CONF_VERIFY_SSL,
    DEFAULT_HEARTBEAT,
    DEFAULT_PORT,
    DEFAULT_PRIORITY,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_STALE_TIMEOUT,
    DOMAIN,
    INSTANCE_UPDATE_TOPIC,
    OPT_DEFAULT_PRIORITY,
    OPT_HEARTBEAT,
    OPT_HIDDEN_EFFECTS,
    OPT_REQUEST_TIMEOUT,
    OPT_STALE_TIMEOUT,
    SIGNAL_INSTANCE_ADDED,
    SIGNAL_INSTANCE_READY,
)
from .coordinator import HyperHdrInstanceCoordinator, HyperHdrRuntimeData, HyperHdrServerCoordinator, _diff_instances
from .entity import server_device_info, server_uid
from .models import HyperHdrServerData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import HyperHdrConfigEntry
    from .models import HyperHdrInstanceSummary

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.BUTTON,
    Platform.CAMERA,
]

# Extra headroom over the client's own request_timeout for the first-connect
# wait in async_setup_entry -- the client's serverinfo call already carries
# its own timeout; this just accounts for scheduling/auth-roundtrip slack.
_CONNECT_WAIT_MARGIN = 5.0

# One lock per config entry, guarding _async_handle_instance_diff so
# overlapping diff runs (e.g. a push arriving while a reconnect's own
# reconciliation is still in flight) can never double-create an instance
# client/coordinator.
_diff_locks: dict[str, asyncio.Lock] = {}


def _get_diff_lock(entry_id: str) -> asyncio.Lock:
    lock = _diff_locks.get(entry_id)
    if lock is None:
        lock = asyncio.Lock()
        _diff_locks[entry_id] = lock
    return lock


def _get_session(hass: HomeAssistant, entry: HyperHdrConfigEntry) -> Any:
    """The aiohttp session for ``entry``: TLS verification follows
    ``verify_ssl`` only when ``use_ssl`` is set (it's meaningless otherwise)."""
    use_ssl = bool(entry.data.get(CONF_USE_SSL, False))
    verify_ssl = entry.data.get(CONF_VERIFY_SSL, True)
    if use_ssl and not verify_ssl:
        return async_get_clientsession(hass, verify_ssl=False)
    return async_get_clientsession(hass)


async def _async_create_instance_client(
    hass: HomeAssistant, entry: HyperHdrConfigEntry, instance_id: int
) -> HyperHdrInstanceClient:
    """Build a fresh, unattached, unstarted instance client.

    Module-level so tests can monkeypatch it. The caller is responsible for
    attaching it to a coordinator and calling ``start()``.

    ``on_auth_failed`` is wired here (not by ``attach_client``, which only
    swaps ``on_connected``/``on_disconnected``) to the same reauth trigger as
    the server client. Safe to call unconditionally -- unlike the server
    client's callback, an instance client is never created before
    ``entry.runtime_data`` exists (every call site is downstream of that
    assignment), so there's no pre-setup race to guard against here.
    """
    session = _get_session(hass, entry)
    options = entry.options

    def _handle_auth_failed() -> None:
        entry.async_start_reauth(hass)

    return HyperHdrInstanceClient(
        session,
        entry.data[CONF_HOST],
        entry.data.get(CONF_PORT, DEFAULT_PORT),
        instance_id=instance_id,
        use_ssl=bool(entry.data.get(CONF_USE_SSL, False)),
        token=entry.data.get(CONF_TOKEN),
        admin_password=entry.data.get(CONF_ADMIN_PASSWORD),
        request_timeout=options.get(OPT_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT),
        heartbeat=options.get(OPT_HEARTBEAT, DEFAULT_HEARTBEAT),
        stale_timeout=options.get(OPT_STALE_TIMEOUT, DEFAULT_STALE_TIMEOUT),
        on_auth_failed=_handle_auth_failed,
    )


async def async_setup_entry(hass: HomeAssistant, entry: HyperHdrConfigEntry) -> bool:
    """Set up HyperHDR from a config entry."""
    session = _get_session(hass, entry)
    options = entry.options
    request_timeout = options.get(OPT_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT)

    connected_event = asyncio.Event()
    last_connect_info: dict[str, Any] = {}
    auth_failed_during_setup = False

    async def _handle_connected(info: dict[str, Any]) -> None:
        nonlocal last_connect_info
        last_connect_info = info
        if not hasattr(entry, "runtime_data"):
            # First connect during setup -- async_setup_entry itself builds
            # the server coordinator once this event resolves.
            connected_event.set()
            return
        # A reconnect after an outage: refresh sysinfo/connected immediately,
        # but deliberately leave `instances` alone here -- the diff handler
        # (below) needs to read it as the pre-reconnect roster to compute
        # what changed while disconnected. It updates `instances` itself.
        runtime = entry.runtime_data
        sysinfo = await runtime.server_client.async_sysinfo()
        roster = HyperHdrServerData.instances_from_roster(info.get("instance", []))
        if runtime.server_coordinator.data is not None:
            runtime.server_coordinator.async_set_updated_data(
                replace(runtime.server_coordinator.data, sysinfo=sysinfo, connected=True)
            )
        await _async_handle_instance_diff(hass, entry, roster)

    def _handle_disconnected() -> None:
        if not hasattr(entry, "runtime_data"):
            return
        runtime = entry.runtime_data
        if runtime.server_coordinator.data is not None:
            runtime.server_coordinator.async_set_updated_data(replace(runtime.server_coordinator.data, connected=False))
        for instance_coordinator in runtime.instance_coordinators.values():
            if instance_coordinator.data is not None:
                instance_coordinator.async_set_updated_data(replace(instance_coordinator.data, connected=False))

    def _handle_auth_failed() -> None:
        nonlocal auth_failed_during_setup
        if not hasattr(entry, "runtime_data"):
            auth_failed_during_setup = True
            connected_event.set()
            return
        entry.async_start_reauth(hass)

    def _handle_instance_update_push(frame: dict[str, Any]) -> None:
        if not hasattr(entry, "runtime_data"):
            # Narrow race: the subscription (sent as part of the connect
            # handshake's own serverinfo call) only goes live once
            # on_connected has already fired, but there's a brief window
            # after that -- before runtime_data is assigned below -- where
            # a real push could still arrive. The roster async_setup_entry
            # is about to seed from already reflects this, so it's safe
            # to drop.
            return
        roster = HyperHdrServerData.instances_from_roster(frame.get("data", []))
        hass.async_create_task(_async_handle_instance_diff(hass, entry, roster))

    client = HyperHdrServerClient(
        session,
        entry.data[CONF_HOST],
        entry.data.get(CONF_PORT, DEFAULT_PORT),
        use_ssl=bool(entry.data.get(CONF_USE_SSL, False)),
        token=entry.data.get(CONF_TOKEN),
        admin_password=entry.data.get(CONF_ADMIN_PASSWORD),
        request_timeout=request_timeout,
        heartbeat=options.get(OPT_HEARTBEAT, DEFAULT_HEARTBEAT),
        stale_timeout=options.get(OPT_STALE_TIMEOUT, DEFAULT_STALE_TIMEOUT),
        on_connected=_handle_connected,
        on_disconnected=_handle_disconnected,
        on_auth_failed=_handle_auth_failed,
    )
    client.set_push_callback(INSTANCE_UPDATE_TOPIC, _handle_instance_update_push)

    await client.start()
    try:
        await asyncio.wait_for(connected_event.wait(), timeout=request_timeout + _CONNECT_WAIT_MARGIN)
    except TimeoutError as err:
        await client.stop()
        raise ConfigEntryNotReady(f"timed out connecting to {entry.data.get(CONF_HOST)}") from err

    if auth_failed_during_setup:
        await client.stop()
        raise ConfigEntryAuthFailed("authentication failed while connecting to HyperHDR")

    sysinfo = await client.async_sysinfo()
    roster = HyperHdrServerData.instances_from_roster(last_connect_info.get("instance", []))

    server_coordinator = HyperHdrServerCoordinator(hass, entry, client)
    server_coordinator.async_set_updated_data(HyperHdrServerData(sysinfo=sysinfo, instances=roster, connected=True))

    entry.runtime_data = HyperHdrRuntimeData(
        server_client=client,
        server_coordinator=server_coordinator,
        instance_coordinators={},
        default_priority=options.get(OPT_DEFAULT_PRIORITY, DEFAULT_PRIORITY),
        hidden_effects=set(options.get(OPT_HIDDEN_EFFECTS, [])),
    )

    # Explicitly register the server device before any entities exist --
    # instance-scoped entities' via_device points at it, and
    # async_forward_entry_setups sets platforms up concurrently, so nothing
    # otherwise guarantees a server-scoped entity (whose own device_info
    # would incidentally create it) gets added first. Idempotent -- a later
    # `HyperHdrServerEntity` (e.g. sensor.py's version sensor) resolves to
    # the same device via the same identifiers.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **server_device_info(server_coordinator, entry)
    )

    running_ids = [instance_id for instance_id, summary in roster.items() if summary.running]
    results = await asyncio.gather(
        *(_async_start_instance(hass, entry, instance_id) for instance_id in running_ids),
        return_exceptions=True,
    )
    for instance_id, result in zip(running_ids, results, strict=True):
        if isinstance(result, BaseException):
            _LOGGER.warning("failed to connect to instance %s during setup: %s", instance_id, result)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: HyperHdrConfigEntry) -> None:
    """Options changed -- reload the entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HyperHdrConfigEntry) -> bool:
    """Unload a config entry. Idempotent -- safe even if clients are already stopped."""
    unload_ok: bool = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None:
        for instance_coordinator in runtime.instance_coordinators.values():
            if instance_coordinator.client is not None:
                await instance_coordinator.client.stop()
        await runtime.server_client.stop()

    _diff_locks.pop(entry.entry_id, None)
    return unload_ok


async def _async_start_instance(hass: HomeAssistant, entry: HyperHdrConfigEntry, instance_id: int) -> None:
    """Handle an instance entering the running state.

    A brand-new instance gets a fresh coordinator (seeded + SIGNAL_INSTANCE_READY
    fired exactly once, at creation); an instance restarting reuses its existing,
    persistent coordinator -- only a fresh client is created and attached.
    """
    runtime = entry.runtime_data
    client = await _async_create_instance_client(hass, entry, instance_id)

    coordinator = runtime.instance_coordinators.get(instance_id)
    is_new = coordinator is None
    if is_new:
        coordinator = HyperHdrInstanceCoordinator(hass, entry, instance_id)
        runtime.instance_coordinators[instance_id] = coordinator

    coordinator.attach_client(client)

    if is_new:
        async_dispatcher_send(hass, f"{SIGNAL_INSTANCE_READY}_{entry.entry_id}", instance_id)

    await client.start()


async def _async_stop_instance(entry: HyperHdrConfigEntry, instance_id: int) -> None:
    """Handle an instance leaving the running state: detach + stop its
    client. The coordinator (and its entities) persists."""
    runtime = entry.runtime_data
    coordinator = runtime.instance_coordinators.get(instance_id)
    if coordinator is None:
        return
    client = coordinator.client
    coordinator.detach_client()
    if client is not None:
        await client.stop()


async def _async_remove_instance(hass: HomeAssistant, entry: HyperHdrConfigEntry, uid: str, instance_id: int) -> None:
    """Handle an instance disappearing from the roster entirely: stop its
    client, drop its coordinator, and purge every trace of it from HA's
    registries."""
    runtime = entry.runtime_data
    coordinator = runtime.instance_coordinators.get(instance_id)
    if coordinator is not None:
        if coordinator.client is not None:
            await coordinator.client.stop()
        runtime.instance_coordinators.pop(instance_id, None)

    prefix = f"{uid}_{instance_id}_"
    entity_registry = er.async_get(hass)
    for entity_entry in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if entity_entry.unique_id.startswith(prefix):
            # NOT a module-level er.async_remove(registry, entity_id) --
            # confirmed live, Phase 5+6: the real homeassistant.helpers.
            # entity_registry module has no such function at all;
            # EntityRegistry.async_remove is an instance method. The
            # previous call raised AttributeError every time an instance
            # was deleted from the server roster, silently swallowed by
            # this being a fire-and-forget task (hass.async_create_task in
            # the instance-update push handler) -- only surfaced as an
            # "Error doing job: Task exception was never retrieved" log
            # entry, never blocking the diff handler's own control flow.
            entity_registry.async_remove(entity_entry.entity_id)

    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(identifiers={(DOMAIN, f"{uid}_{instance_id}")})
    if device is not None:
        device_registry.async_remove_device(device.id)


async def _async_handle_instance_diff(
    hass: HomeAssistant, entry: HyperHdrConfigEntry, new_roster: dict[int, HyperHdrInstanceSummary]
) -> None:
    """Reconcile a fresh instance roster against current state.

    Module-level with explicit args for testability. Serialized per config
    entry via ``_get_diff_lock`` so overlapping calls (a push arriving mid
    reconciliation, say) can never double-create a client/coordinator.
    """
    runtime = entry.runtime_data
    async with _get_diff_lock(entry.entry_id):
        old_roster = runtime.server_coordinator.data.instances if runtime.server_coordinator.data else {}
        diff = _diff_instances(old_roster, new_roster)

        if runtime.server_coordinator.data is not None:
            runtime.server_coordinator.async_set_updated_data(
                replace(runtime.server_coordinator.data, instances=new_roster)
            )

        uid = server_uid(entry)
        for instance_id in diff.created:
            async_dispatcher_send(hass, f"{SIGNAL_INSTANCE_ADDED}_{entry.entry_id}", instance_id)
        for instance_id in diff.started:
            await _async_start_instance(hass, entry, instance_id)
        for instance_id in diff.stopped:
            await _async_stop_instance(entry, instance_id)
        for instance_id in diff.removed:
            await _async_remove_instance(hass, entry, uid, instance_id)
