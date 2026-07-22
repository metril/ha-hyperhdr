"""Switch platform for the HyperHDR integration.

Two switch kinds, both built dynamically rather than as a fixed list:

- One component switch per entry in ``data.components`` (CONFIG category --
  full control over every reported component, including ``ALL`` and
  ``LEDDEVICE``, which mirrors the light's own power state). Added on
  ``SIGNAL_INSTANCE_READY`` like every other data-driven entity.
- One "running" switch per *roster* instance (no entity_category). Unlike
  every other entity in this integration it does NOT need an instance
  coordinator to exist yet -- its state/commands are server-scoped (the
  roster + start/stop), so it's added on ``SIGNAL_INSTANCE_ADDED`` instead,
  which fires for a freshly created instance even before it's ever started.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory

from .const import COMPONENT_LABELS, SIGNAL_INSTANCE_ADDED, SIGNAL_INSTANCE_READY
from .entity import (
    HyperHdrInstanceEntity,
    HyperHdrServerEntity,
    instance_device_info,
    require_instance_client,
    wait_for_connected_data,
)
from .exceptions import HyperHdrError

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .client import HyperHdrServerClient
    from .coordinator import HyperHdrConfigEntry, HyperHdrInstanceCoordinator, HyperHdrServerCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: HyperHdrConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up HyperHDR switch entities."""
    runtime = entry.runtime_data

    component_lists = await asyncio.gather(
        *(
            _component_entities_for_instance(entry, coordinator, instance_id)
            for instance_id, coordinator in runtime.instance_coordinators.items()
        )
    )
    entities: list[SwitchEntity] = [switch for sublist in component_lists for switch in sublist]
    server_data = runtime.server_coordinator.data
    if server_data is not None:
        entities.extend(
            HyperHdrRunningSwitch(runtime.server_coordinator, entry, instance_id)
            for instance_id in server_data.instances
        )
    async_add_entities(entities)

    async def _add_components_for_instance(instance_id: int) -> None:
        coordinator = runtime.instance_coordinators[instance_id]
        async_add_entities(await _component_entities_for_instance(entry, coordinator, instance_id))

    async def _add_running_for_instance(instance_id: int) -> None:
        async_add_entities([HyperHdrRunningSwitch(runtime.server_coordinator, entry, instance_id)])

    entry.async_on_unload(
        async_dispatcher_connect(hass, f"{SIGNAL_INSTANCE_READY}_{entry.entry_id}", _add_components_for_instance)
    )
    entry.async_on_unload(
        async_dispatcher_connect(hass, f"{SIGNAL_INSTANCE_ADDED}_{entry.entry_id}", _add_running_for_instance)
    )


async def _component_entities_for_instance(
    entry: HyperHdrConfigEntry, coordinator: HyperHdrInstanceCoordinator, instance_id: int
) -> list[HyperHdrComponentSwitch]:
    """Build one switch per reported component.

    Bounded-waits for a connected snapshot first (``wait_for_connected_data``)
    -- ``coordinator.data.components`` is an empty placeholder until the
    instance's client finishes connecting, and unlike an entity's *state*
    (read fresh on every access), the entity *set* built here is a one-time
    snapshot: building it too early would silently create zero switches,
    permanently, for this add pass. Confirmed live (Phase 5+6).
    """
    data = await wait_for_connected_data(coordinator)
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
        self._attr_name = COMPONENT_LABELS.get(component_name, component_name.title())

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
            raise HomeAssistantError(f"failed to set HyperHDR component {self._component_name}: {err}") from err


class HyperHdrRunningSwitch(HyperHdrServerEntity, SwitchEntity):
    """Start/stop a HyperHDR instance.

    Server-scoped (state comes from the roster on ``HyperHdrServerCoordinator``
    -- a created-but-not-started instance has no ``HyperHdrInstanceCoordinator``
    at all yet) but grouped onto the instance's own device via
    ``instance_device_info``.
    """

    _attr_name = "Running"

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

    @property
    def available(self) -> bool:
        """Unavailable while the server connection itself is down."""
        data_connected = self.coordinator.data.connected if self.coordinator.data is not None else False
        return bool(super().available) and data_connected

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start the instance."""
        try:
            await self._server_client.start_instance(self._instance_id)
        except HyperHdrError as err:
            raise HomeAssistantError(f"failed to start HyperHDR instance {self._instance_id}: {err}") from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the instance."""
        try:
            await self._server_client.stop_instance(self._instance_id)
        except HyperHdrError as err:
            raise HomeAssistantError(f"failed to stop HyperHDR instance {self._instance_id}: {err}") from err
