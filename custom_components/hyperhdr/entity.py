"""Base entity classes for the HyperHDR integration.

Two scopes, matching the two coordinators in coordinator.py: a server-scoped
base (one device per HyperHDR server) and an instance-scoped base (one
device per HyperHDR instance, ``via_device``-linked to the server device).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_USE_SSL, DEFAULT_PORT, DOMAIN, MANUFACTURER
from .coordinator import HyperHdrInstanceCoordinator, HyperHdrServerCoordinator

if TYPE_CHECKING:
    from .coordinator import HyperHdrConfigEntry


def server_uid(entry: HyperHdrConfigEntry) -> str:
    """The stable id identifying this config entry's HyperHDR server.

    Falls back to ``entry_id`` for entries that predate a ``unique_id``
    (or in tests that never assign one) -- used as the device identifier
    and as the unique_id prefix for every entity this integration creates.
    """
    unique_id: str | None = entry.unique_id
    entry_id: str = entry.entry_id
    return unique_id or entry_id


class HyperHdrServerEntity(CoordinatorEntity[HyperHdrServerCoordinator]):
    """Base for server-scoped entities (one device per HyperHDR server)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HyperHdrServerCoordinator, entry: HyperHdrConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        uid = server_uid(entry)
        self._attr_unique_id = f"{uid}_{key}"

        sysinfo = coordinator.data.sysinfo if coordinator.data is not None else None
        scheme = "https" if entry.data.get(CONF_USE_SSL) else "http"
        host = entry.data.get(CONF_HOST, "")
        port = entry.data.get(CONF_PORT, DEFAULT_PORT)

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, uid)},
            manufacturer=MANUFACTURER,
            name=sysinfo.hostname if sysinfo and sysinfo.hostname else entry.title,
            sw_version=sysinfo.version if sysinfo else None,
            configuration_url=f"{scheme}://{host}:{port}",
        )


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

        server_coordinator = entry.runtime_data.server_coordinator
        summary = server_coordinator.data.instances.get(instance_id) if server_coordinator.data else None
        name = summary.friendly_name if summary and summary.friendly_name else f"Instance {instance_id}"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{uid}_{instance_id}")},
            via_device=(DOMAIN, uid),
            manufacturer=MANUFACTURER,
            name=name,
        )

    @property
    def available(self) -> bool:
        """Unavailable while the instance's coordinator reports disconnected
        (server unreachable, or the instance itself stopped)."""
        data_connected: bool = self.coordinator.data.connected
        return bool(super().available) and data_connected
