"""Base entity classes for the HyperHDR integration.

Two scopes, matching the two coordinators in coordinator.py: a server-scoped
base (one device per HyperHDR server) and an instance-scoped base (one
device per HyperHDR instance, ``via_device``-linked to the server device).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_USE_SSL, DEFAULT_PORT, DOMAIN, MANUFACTURER
from .coordinator import HyperHdrInstanceCoordinator, HyperHdrServerCoordinator

if TYPE_CHECKING:
    from .client import HyperHdrInstanceClient
    from .coordinator import HyperHdrConfigEntry
    from .models import HyperHdrInstanceData

# Bounded wait for wait_for_connected_data -- generous for a real (even
# slow/remote) HyperHDR server while never hanging platform setup
# indefinitely against one that's genuinely unreachable (matches the same
# order of magnitude as __init__.py's own first-connect wait).
_CONNECTED_DATA_WAIT_TIMEOUT = 10.0


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
    platform setup -- instance-scoped entities' ``via_device`` points at it,
    and ``hass.config_entries.async_forward_entry_setups`` sets platforms up
    concurrently, so which platform's entities land first (and would
    otherwise be the one to incidentally create this device) is not
    deterministic. Observed live: without this, whichever of light/switch/
    select's entities got added before sensor.py's server-scoped ``version``
    sensor logged a "referencing a non existing via_device" warning.
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
        via_device=(DOMAIN, uid),
        manufacturer=MANUFACTURER,
        name=name,
    )


def require_instance_client(coordinator: HyperHdrInstanceCoordinator) -> HyperHdrInstanceClient:
    """The instance coordinator's currently attached client, for entity
    service calls (Phase 5+6).

    Raises ``HomeAssistantError`` (never a bare ``AttributeError``) when the
    instance is currently stopped/disconnected -- ``coordinator.client`` is
    ``None`` whenever ``detach_client`` has run and no ``attach_client`` has
    happened since (see coordinator.py).
    """
    if coordinator.client is None:
        raise HomeAssistantError("HyperHDR instance is not connected")
    return coordinator.client


async def wait_for_connected_data(
    coordinator: HyperHdrInstanceCoordinator, timeout: float = _CONNECTED_DATA_WAIT_TIMEOUT
) -> HyperHdrInstanceData:
    """The coordinator's first genuinely connected snapshot, bounded-waited.

    For entity platforms whose *set* of entities (not just their state) is
    built from live data content -- switch.py's per-component switches,
    number.py's presence-gated adjustment fields -- reading
    ``coordinator.data`` at build time is NOT safe to do immediately:
    ``SIGNAL_INSTANCE_READY`` fires (and the initial per-platform
    ``async_setup_entry`` loop can run) before the just-attached client has
    actually finished its connect handshake, per coordinator.py's own
    ``_initial_instance_data`` docstring. Building against that disconnected
    placeholder (empty ``components``/``adjustment.raw``) would silently
    create ZERO of these entities, permanently, for that add pass -- no
    later mechanism re-adds them once the real data arrives, since
    CoordinatorEntity only pushes state updates to already-created entities.

    Confirmed live (Phase 5+6): omitting this wait reliably produced zero
    live component switches/number entities on a fresh HA start (verified
    via ``"restored": true`` ghost entities in ``/api/states`` -- the
    registry remembered them from a prior run, but no live entity backed
    them this time).

    Falls back to whatever ``coordinator.data`` currently holds (even if
    still disconnected) once ``timeout`` elapses, matching every other
    entity's own graceful handling of a slow/failed connection -- entities
    just end up unavailable rather than setup hanging forever.
    """
    if coordinator.data.connected:
        return coordinator.data

    became_connected = asyncio.Event()

    def _on_update() -> None:
        if coordinator.data is not None and coordinator.data.connected:
            became_connected.set()

    unsubscribe = coordinator.async_add_listener(_on_update)
    try:
        await asyncio.wait_for(became_connected.wait(), timeout=timeout)
    except TimeoutError:
        pass
    finally:
        unsubscribe()
    return coordinator.data


class HyperHdrServerEntity(CoordinatorEntity[HyperHdrServerCoordinator]):
    """Base for server-scoped entities (one device per HyperHDR server)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HyperHdrServerCoordinator, entry: HyperHdrConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        uid = server_uid(entry)
        self._attr_unique_id = f"{uid}_{key}"
        self._attr_device_info = server_device_info(coordinator, entry)


class HyperHdrInstanceEntity(CoordinatorEntity[HyperHdrInstanceCoordinator]):
    """Base for instance-scoped entities (one device per HyperHDR instance,
    linked to the server device via ``via_device``)."""

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
