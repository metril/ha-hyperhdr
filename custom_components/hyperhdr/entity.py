"""Base entity classes for the HyperHDR integration.

Two scopes, matching the two coordinators in coordinator.py: a server-scoped
base (one device per HyperHDR server) and an instance-scoped base (one
device per HyperHDR instance, ``via_device_id``-linked to the server device).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_USE_SSL, DEFAULT_PORT, DOMAIN, MANUFACTURER, SIGNAL_INSTANCE_READY
from .coordinator import HyperHdrInstanceCoordinator, HyperHdrServerCoordinator
from .exceptions import translated_error

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .client import HyperHdrInstanceClient
    from .coordinator import HyperHdrConfigEntry
    from .models import HyperHdrInstanceData


def server_uid(entry: HyperHdrConfigEntry) -> str:
    """The stable id identifying this config entry's HyperHDR server.

    Falls back to ``entry_id`` for entries that predate a ``unique_id``
    (or in tests that never assign one) -- used as the device identifier
    and as the unique_id prefix for every entity this integration creates.
    """
    unique_id: str | None = entry.unique_id
    entry_id: str = entry.entry_id
    return unique_id or entry_id


def server_device_info(coordinator: HyperHdrServerCoordinator, entry: HyperHdrConfigEntry) -> DeviceInfo:
    """Build the ``DeviceInfo`` for the HyperHDR server device.

    Extracted (Phase 5+6, alongside ``instance_device_info``) so ``__init__.py``
    can explicitly register this device in the registry *before* forwarding
    platform setup -- instance-scoped entities' ``via_device_id`` points at it,
    and ``hass.config_entries.async_forward_entry_setups`` sets platforms up
    concurrently, so which platform's entities land first (and would
    otherwise be the one to incidentally create this device) is not
    deterministic. Observed live: without this, whichever of light/switch/
    select's entities got added before sensor.py's server-scoped ``version``
    sensor logged a "referencing a non existing via_device_id" warning.
    """
    uid = server_uid(entry)
    sysinfo = coordinator.data.sysinfo if coordinator.data is not None else None
    scheme = "https" if entry.data.get(CONF_USE_SSL) else "http"
    host = entry.data.get(CONF_HOST, "")
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)

    return DeviceInfo(
        identifiers={(DOMAIN, uid)},
        manufacturer=MANUFACTURER,
        name=sysinfo.hostname if sysinfo and sysinfo.hostname else entry.title,
        sw_version=sysinfo.version if sysinfo else None,
        configuration_url=f"{scheme}://{host}:{port}",
    )


def instance_device_info(entry: HyperHdrConfigEntry, instance_id: int) -> DeviceInfo:
    """Build the ``DeviceInfo`` for a HyperHDR instance device.

    Extracted (Phase 5+6) so both ``HyperHdrInstanceEntity`` (whose data
    comes from the instance coordinator) and a server-scoped entity that
    still wants to live on the instance's device can share it -- namely
    switch.py's "running" switch, whose on/off state comes from the
    server-scoped instance roster (so it can exist for a created-but-not-
    yet-started instance, before any ``HyperHdrInstanceCoordinator`` exists
    for it) but which should still show up grouped under the instance's own
    device in the UI, not the server's.
    """
    uid = server_uid(entry)
    server_coordinator = entry.runtime_data.server_coordinator
    summary = server_coordinator.data.instances.get(instance_id) if server_coordinator.data else None
    name = summary.friendly_name if summary and summary.friendly_name else f"Instance {instance_id}"

    return DeviceInfo(
        identifiers={(DOMAIN, f"{uid}_{instance_id}")},
        via_device_id=entry.runtime_data.server_device_id,
        manufacturer=MANUFACTURER,
        name=name,
    )


def require_instance_client(coordinator: HyperHdrInstanceCoordinator) -> HyperHdrInstanceClient:
    """The instance coordinator's currently attached client, for entity
    service calls (Phase 5+6).

    Raises a translated ``HomeAssistantError`` (never a bare ``AttributeError``) when the
    instance is currently stopped/disconnected -- ``coordinator.client`` is
    ``None`` whenever ``detach_client`` has run and no ``attach_client`` has
    happened since (see coordinator.py).
    """
    if coordinator.client is None:
        raise translated_error("instance_disconnected")
    return coordinator.client


async def async_setup_connected_entities(
    hass: HomeAssistant,
    entry: HyperHdrConfigEntry,
    async_add_entities: AddEntitiesCallback,
    build: Callable[[HyperHdrConfigEntry, HyperHdrInstanceData, HyperHdrInstanceCoordinator, int], list[Any]],
) -> None:
    """Add entities whose *set* depends on live instance data, as it appears.

    ``build(entry, data, coordinator, instance_id)`` is evaluated against every
    *connected* snapshot of each instance's coordinator (never the
    disconnected placeholder, which has empty components/adjustment/LED
    geometry); entities whose ``unique_id`` was already added are skipped,
    so a snapshot that arrives late (after a slow connect) or that reports
    something new still adds the missing entities, exactly once.

    The ``SIGNAL_INSTANCE_READY`` listener is connected before anything is
    inspected, and per-coordinator tracking is idempotent, so an instance
    created while this runs is never missed nor double-handled.
    """
    runtime = entry.runtime_data
    tracked: dict[int, HyperHdrInstanceCoordinator] = {}
    added: dict[int, set[str]] = {}

    def _add_missing(coordinator: HyperHdrInstanceCoordinator, instance_id: int) -> None:
        seen = added.setdefault(instance_id, set())
        new = [e for e in build(entry, coordinator.data, coordinator, instance_id) if e.unique_id not in seen]
        if new:
            seen.update(e.unique_id for e in new)
            async_add_entities(new)

    async def _track(instance_id: int) -> None:
        coordinator = runtime.instance_coordinators.get(instance_id)
        if coordinator is None or tracked.get(instance_id) is coordinator:
            return
        if instance_id in tracked:  # instance deleted and re-created: fresh coordinator, fresh registry
            added.pop(instance_id, None)
        tracked[instance_id] = coordinator

        def _on_update() -> None:
            if coordinator.data is not None and coordinator.data.connected:
                _add_missing(coordinator, instance_id)

        entry.async_on_unload(coordinator.async_add_listener(_on_update))
        _on_update()

    entry.async_on_unload(async_dispatcher_connect(hass, f"{SIGNAL_INSTANCE_READY}_{entry.entry_id}", _track))
    for instance_id in list(runtime.instance_coordinators):
        await _track(instance_id)


class HyperHdrServerEntity(CoordinatorEntity[HyperHdrServerCoordinator]):
    """Base for server-scoped entities (one device per HyperHDR server)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HyperHdrServerCoordinator, entry: HyperHdrConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        uid = server_uid(entry)
        self._attr_unique_id = f"{uid}_{key}"
        self._attr_device_info = server_device_info(coordinator, entry)

    @property
    def available(self) -> bool:
        """Unavailable while the server connection itself is down."""
        data = self.coordinator.data
        return bool(super().available) and data is not None and data.connected


class HyperHdrInstanceEntity(CoordinatorEntity[HyperHdrInstanceCoordinator]):
    """Base for instance-scoped entities (one device per HyperHDR instance,
    linked to the server device via ``via_device_id``)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HyperHdrInstanceCoordinator,
        entry: HyperHdrConfigEntry,
        instance_id: int,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        uid = server_uid(entry)
        self._attr_unique_id = f"{uid}_{instance_id}_{key}"
        self._attr_device_info = instance_device_info(entry, instance_id)

    @property
    def available(self) -> bool:
        """Unavailable while the instance's coordinator reports disconnected
        (server unreachable, or the instance itself stopped)."""
        data_connected: bool = self.coordinator.data.connected
        return bool(super().available) and data_connected
