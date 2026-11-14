"""Button platform for the HyperHDR integration -- one entity per instance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import SIGNAL_INSTANCE_READY
from .entity import HyperHdrInstanceEntity, require_instance_client
from .exceptions import HyperHdrError, translated_error

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import HyperHdrConfigEntry, HyperHdrInstanceCoordinator

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: HyperHdrConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up HyperHDR button entities."""
    runtime = entry.runtime_data
    entities: list[HyperHdrClearPriorityButton] = []
    for instance_id, coordinator in runtime.instance_coordinators.items():
        entities.extend(_entities_for_instance(entry, coordinator, instance_id))
    async_add_entities(entities)
    added: set[str | None] = {entity.unique_id for entity in entities}

    async def _add_for_instance(instance_id: int) -> None:
        coordinator = runtime.instance_coordinators.get(instance_id)
        if coordinator is None:
            return
        fresh = [e for e in _entities_for_instance(entry, coordinator, instance_id) if e.unique_id not in added]
        added.update(entity.unique_id for entity in fresh)
        async_add_entities(fresh)

    entry.async_on_unload(
        async_dispatcher_connect(hass, f"{SIGNAL_INSTANCE_READY}_{entry.entry_id}", _add_for_instance)
    )


def _entities_for_instance(
    entry: HyperHdrConfigEntry, coordinator: HyperHdrInstanceCoordinator, instance_id: int
) -> list[HyperHdrClearPriorityButton]:
    return [HyperHdrClearPriorityButton(coordinator, entry, instance_id)]


class HyperHdrClearPriorityButton(HyperHdrInstanceEntity, ButtonEntity):
    """Clear this integration's own priority (``runtime_data.default_priority``)."""

    _attr_translation_key = "clear_priority"

    def __init__(self, coordinator: HyperHdrInstanceCoordinator, entry: HyperHdrConfigEntry, instance_id: int) -> None:
        """Initialize the button."""
        super().__init__(coordinator, entry, instance_id, "clear_priority")
        self._default_priority = entry.runtime_data.default_priority

    async def async_press(self) -> None:
        """Clear ``default_priority`` on the HyperHDR instance."""
        client = require_instance_client(self.coordinator)
        try:
            await client.async_clear(self._default_priority)
        except HyperHdrError as err:
            raise translated_error("clear_failed", priority=self._default_priority, error=err) from err
