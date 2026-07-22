"""Sensor platform for the HyperHDR integration.

EntityDescription pattern (``value_fn``, matching the house ha-awtrix
style). Per instance: ``visible_priority``, ``led_count`` (DIAGNOSTIC),
``video_mode`` (DIAGNOSTIC). Server-scoped: ``version`` (DIAGNOSTIC, from
sysinfo).

No ``average_color`` sensor: it needs a polled API call HyperHDR doesn't
push, and this integration is push-only by design -- deliberately out of
scope for v1 (see ``docs/api-notes.md``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory

from .const import SIGNAL_INSTANCE_READY
from .entity import HyperHdrInstanceEntity, HyperHdrServerEntity

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import HyperHdrConfigEntry, HyperHdrInstanceCoordinator, HyperHdrServerCoordinator
    from .models import HyperHdrInstanceData, HyperHdrPriority


def _visible_priority(data: HyperHdrInstanceData) -> HyperHdrPriority | None:
    for priority in data.priorities:
        if priority.visible:
            return priority
    return None


def _visible_priority_state(data: HyperHdrInstanceData) -> str | None:
    priority = _visible_priority(data)
    if priority is None:
        return None
    return priority.origin or priority.component_id or None


def _visible_priority_attrs(data: HyperHdrInstanceData) -> dict[str, Any]:
    priority = _visible_priority(data)
    if priority is None:
        return {}
    return {
        "priority": priority.priority,
        "component_id": priority.component_id,
        "rgb": priority.rgb,
        "owner": priority.owner,
    }


@dataclass(frozen=True, kw_only=True)
class HyperHdrSensorDescription(SensorEntityDescription):
    """Describes a HyperHDR sensor entity."""

    value_fn: Callable[[Any], Any]
    attrs_fn: Callable[[Any], dict[str, Any]] | None = None


INSTANCE_SENSORS: tuple[HyperHdrSensorDescription, ...] = (
    HyperHdrSensorDescription(
        key="visible_priority",
        name="Visible priority",
        value_fn=_visible_priority_state,
        attrs_fn=_visible_priority_attrs,
    ),
    HyperHdrSensorDescription(
        key="led_count",
        name="LED count",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=None,
        value_fn=lambda data: data.led_count,
    ),
    HyperHdrSensorDescription(
        key="video_mode",
        name="Video mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.video_mode or None,
    ),
)

SERVER_SENSORS: tuple[HyperHdrSensorDescription, ...] = (
    HyperHdrSensorDescription(
        key="version",
        name="Version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.sysinfo.version or None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: HyperHdrConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up HyperHDR sensor entities."""
    runtime = entry.runtime_data
    entities: list[SensorEntity] = [
        HyperHdrServerSensor(runtime.server_coordinator, entry, description) for description in SERVER_SENSORS
    ]
    for instance_id, coordinator in runtime.instance_coordinators.items():
        entities.extend(_entities_for_instance(entry, coordinator, instance_id))
    async_add_entities(entities)

    async def _add_for_instance(instance_id: int) -> None:
        coordinator = runtime.instance_coordinators[instance_id]
        async_add_entities(_entities_for_instance(entry, coordinator, instance_id))

    entry.async_on_unload(
        async_dispatcher_connect(hass, f"{SIGNAL_INSTANCE_READY}_{entry.entry_id}", _add_for_instance)
    )


def _entities_for_instance(
    entry: HyperHdrConfigEntry, coordinator: HyperHdrInstanceCoordinator, instance_id: int
) -> list[HyperHdrInstanceSensor]:
    return [HyperHdrInstanceSensor(coordinator, entry, instance_id, description) for description in INSTANCE_SENSORS]


class HyperHdrInstanceSensor(HyperHdrInstanceEntity, SensorEntity):
    """An instance-scoped sensor driven by a ``HyperHdrSensorDescription``."""

    entity_description: HyperHdrSensorDescription

    def __init__(
        self,
        coordinator: HyperHdrInstanceCoordinator,
        entry: HyperHdrConfigEntry,
        instance_id: int,
        description: HyperHdrSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, instance_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """The sensor's value, from ``entity_description.value_fn``."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Extra attributes, from ``entity_description.attrs_fn`` if set."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)


class HyperHdrServerSensor(HyperHdrServerEntity, SensorEntity):
    """A server-scoped sensor driven by a ``HyperHdrSensorDescription``."""

    entity_description: HyperHdrSensorDescription

    def __init__(
        self,
        coordinator: HyperHdrServerCoordinator,
        entry: HyperHdrConfigEntry,
        description: HyperHdrSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """The sensor's value, from ``entity_description.value_fn``."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
