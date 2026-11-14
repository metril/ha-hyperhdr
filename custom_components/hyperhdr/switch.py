"""Switch platform for the HyperHDR integration.

Two switch kinds, both built dynamically rather than as a fixed list:

- One component switch per entry in ``data.components`` (CONFIG category --
  full control over every reported component). Added on
  ``SIGNAL_INSTANCE_READY`` like every other data-driven entity. Two
  components deserve a note: ``LEDDEVICE`` is the same component the light
  entity's on/off drives (the two mirror each other), and ``ALL`` is
  server-GLOBAL -- HyperHDR routes it to ``toggleStateAllInstances``, which
  pauses/resumes every running instance, hence its "All instances (global)"
  label (HyperHDR's own web-UI wording).
- One "running" switch per *roster* instance (no entity_category) -- except
  instance ``FIRST_INSTANCE_ID``, which never gets one: HyperHDR forbids
  stopping/deleting instance 0 (``isInstAllowed`` is ``inst > 0``) and acks
  its ``stopInstance`` as a silent no-op success, so the switch could never
  work in either direction there. Unlike every other entity in this
  integration it does NOT need an instance coordinator to exist yet -- its
  state/commands are server-scoped (the roster + start/stop), so it's added
  on ``SIGNAL_INSTANCE_ADDED`` instead, which fires for a freshly created
  instance even before it's ever started.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory

from .const import (
    COMPONENT_ICONS,
    COMPONENT_LABELS,
    FIRST_INSTANCE_ID,
    SIGNAL_INSTANCE_ADDED,
)
from .entity import (
    HyperHdrInstanceEntity,
    HyperHdrServerEntity,
    async_setup_connected_entities,
    instance_device_info,
    require_instance_client,
)
from .exceptions import HyperHdrError, translated_error

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .client import HyperHdrServerClient
    from .coordinator import HyperHdrConfigEntry, HyperHdrInstanceCoordinator, HyperHdrServerCoordinator
    from .models import HyperHdrInstanceData

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: HyperHdrConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up HyperHDR switch entities."""
    runtime = entry.runtime_data
    await async_setup_connected_entities(hass, entry, async_add_entities, _component_entities)

    server_data = runtime.server_coordinator.data
    if server_data is not None:
        async_add_entities(
            HyperHdrRunningSwitch(runtime.server_coordinator, entry, instance_id)
            for instance_id in server_data.instances
            if instance_id != FIRST_INSTANCE_ID
        )

    async def _add_running_for_instance(instance_id: int) -> None:
        if instance_id == FIRST_INSTANCE_ID:
            return
        async_add_entities([HyperHdrRunningSwitch(runtime.server_coordinator, entry, instance_id)])

    entry.async_on_unload(
        async_dispatcher_connect(hass, f"{SIGNAL_INSTANCE_ADDED}_{entry.entry_id}", _add_running_for_instance)
    )


def _component_entities(
    entry: HyperHdrConfigEntry, data: HyperHdrInstanceData, coordinator: HyperHdrInstanceCoordinator, instance_id: int
) -> list[HyperHdrComponentSwitch]:
    """One switch per component in a *connected* snapshot.

    Evaluated on every connected update by ``async_setup_connected_entities``
    (which dedupes by unique_id), so components reported only by a late
    first snapshot -- or appearing later -- still get their switch.
    """
    return [HyperHdrComponentSwitch(coordinator, entry, instance_id, name) for name in data.components]


class HyperHdrComponentSwitch(HyperHdrInstanceEntity, SwitchEntity):
    """Enable/disable one HyperHDR component."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: HyperHdrInstanceCoordinator,
        entry: HyperHdrConfigEntry,
        instance_id: int,
        component_name: str,
    ) -> None:
        """Initialize the component switch."""
        super().__init__(coordinator, entry, instance_id, f"component_{component_name.lower()}")
        self._component_name = component_name
        if component_name in COMPONENT_LABELS:
            self._attr_translation_key = component_name.lower()
        else:
            self._attr_name = component_name.title()
        self._attr_icon = COMPONENT_ICONS.get(component_name)

    @property
    def is_on(self) -> bool:
        """Whether this component is currently enabled."""
        component = self.coordinator.data.components.get(self._component_name)
        return component.enabled if component is not None else False

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the component."""
        await self._async_set_state(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the component."""
        await self._async_set_state(False)

    async def _async_set_state(self, state: bool) -> None:
        client = require_instance_client(self.coordinator)
        try:
            await client.async_set_component(self._component_name, state)
        except HyperHdrError as err:
            raise translated_error("component_failed", component=self._component_name, error=err) from err


class HyperHdrRunningSwitch(HyperHdrServerEntity, SwitchEntity):
    """Start/stop a HyperHDR instance.

    Server-scoped (state comes from the roster on ``HyperHdrServerCoordinator``
    -- a created-but-not-started instance has no ``HyperHdrInstanceCoordinator``
    at all yet) but grouped onto the instance's own device via
    ``instance_device_info``. Never built for ``FIRST_INSTANCE_ID`` -- see the
    module docstring.
    """

    _attr_translation_key = "running"
    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, coordinator: HyperHdrServerCoordinator, entry: HyperHdrConfigEntry, instance_id: int) -> None:
        """Initialize the running switch."""
        super().__init__(coordinator, entry, f"{instance_id}_running")
        self._instance_id = instance_id
        self._server_client: HyperHdrServerClient = entry.runtime_data.server_client
        self._attr_device_info = instance_device_info(entry, instance_id)

    @property
    def is_on(self) -> bool:
        """Whether this instance is currently running, per the roster."""
        if self.coordinator.data is None:
            return False
        summary = self.coordinator.data.instances.get(self._instance_id)
        return summary.running if summary is not None else False

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start the instance."""
        try:
            await self._server_client.start_instance(self._instance_id)
        except HyperHdrError as err:
            raise translated_error("start_instance_failed", instance_id=self._instance_id, error=err) from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the instance."""
        try:
            await self._server_client.stop_instance(self._instance_id)
        except HyperHdrError as err:
            raise translated_error("stop_instance_failed", instance_id=self._instance_id, error=err) from err
