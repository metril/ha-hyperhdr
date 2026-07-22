"""Select platform for the HyperHDR integration -- two entities per instance.

No smoothing-type select: smoothing config is only reachable through the
admin-gated ``config``/``getconfig`` call and its write path
(``config``/``setconfig``) is unverified against the live server (see
``docs/api-notes.md``) -- deliberately out of scope for v1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory

from .const import HDR_MODE_OFF, HDR_MODE_ON, SIGNAL_INSTANCE_READY, SOURCE_AUTO
from .entity import HyperHdrInstanceEntity, require_instance_client
from .exceptions import HyperHdrError

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import HyperHdrConfigEntry, HyperHdrInstanceCoordinator

_HDR_OPTIONS = ("off", "on")
_HDR_OPTION_TO_MODE: dict[str, int] = {"off": HDR_MODE_OFF, "on": HDR_MODE_ON}
_HDR_MODE_TO_OPTION: dict[int, str] = {mode: option for option, mode in _HDR_OPTION_TO_MODE.items()}


async def async_setup_entry(
    hass: HomeAssistant, entry: HyperHdrConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up HyperHDR select entities."""
    runtime = entry.runtime_data
    entities: list[SelectEntity] = []
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
) -> list[SelectEntity]:
    return [
        HyperHdrHdrToneMappingSelect(coordinator, entry, instance_id),
        HyperHdrPrioritySourceSelect(coordinator, entry, instance_id),
    ]


class HyperHdrHdrToneMappingSelect(HyperHdrInstanceEntity, SelectEntity):
    """Toggle HDR tone mapping (``videomodehdr`` 0/1).

    v22 exposes no third/auto mode; any other integer value ``hdr_mode``
    might carry is represented as an unmapped (``None``) current option
    rather than guessed at.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "HDR tone mapping"
    _attr_options = list(_HDR_OPTIONS)

    def __init__(self, coordinator: HyperHdrInstanceCoordinator, entry: HyperHdrConfigEntry, instance_id: int) -> None:
        """Initialize the HDR tone mapping select."""
        super().__init__(coordinator, entry, instance_id, "hdr_tone_mapping")

    @property
    def current_option(self) -> str | None:
        """The option matching ``data.hdr_mode``, or None if unrecognized."""
        return _HDR_MODE_TO_OPTION.get(self.coordinator.data.hdr_mode)

    async def async_select_option(self, option: str) -> None:
        """Set HDR tone mapping to the selected option.

        Optimistically publishes the new ``hdr_mode`` through the
        coordinator on success (Phase 7+8 fix) -- HyperHDR pushes no
        ``videomode-update`` for this transition, so without it the select
        would revert to the pre-toggle option until the next reconnect. See
        ``HyperHdrInstanceCoordinator.apply_optimistic_hdr_mode``.
        """
        client = require_instance_client(self.coordinator)
        mode = _HDR_OPTION_TO_MODE[option]
        try:
            await client.async_set_hdr_mode(mode)
        except HyperHdrError as err:
            raise HomeAssistantError(f"failed to set HyperHDR HDR tone mapping: {err}") from err
        self.coordinator.apply_optimistic_hdr_mode(mode)


class HyperHdrPrioritySourceSelect(HyperHdrInstanceEntity, SelectEntity):
    """Pin (or auto-select) the visible priority source.

    No entity_category -- this is a primary control, not config/diagnostic.
    """

    _attr_name = "Priority source"

    def __init__(self, coordinator: HyperHdrInstanceCoordinator, entry: HyperHdrConfigEntry, instance_id: int) -> None:
        """Initialize the priority source select."""
        super().__init__(coordinator, entry, instance_id, "priority_source")

    @property
    def options(self) -> list[str]:
        """``Auto`` plus one label per currently active priority, rebuilt
        from live data on every read."""
        return [SOURCE_AUTO] + [
            f"{priority.priority}: {priority.component_id} ({priority.origin})"
            for priority in self.coordinator.data.priorities
            if priority.active
        ]

    @property
    def current_option(self) -> str | None:
        """``Auto`` when autoselect is on, else the visible priority's
        label, else None."""
        if self.coordinator.data.priorities_autoselect:
            return SOURCE_AUTO
        for priority in self.coordinator.data.priorities:
            if priority.visible:
                return f"{priority.priority}: {priority.component_id} ({priority.origin})"
        return None

    async def async_select_option(self, option: str) -> None:
        """Select ``Auto`` (clears any manual pin) or a specific priority
        (parsed from the option's leading integer)."""
        client = require_instance_client(self.coordinator)
        try:
            if option == SOURCE_AUTO:
                await client.async_select_source(None)
            else:
                priority = int(option.split(":", 1)[0])
                await client.async_select_source(priority)
        except (HyperHdrError, ValueError) as err:
            raise HomeAssistantError(f"failed to select HyperHDR priority source {option!r}: {err}") from err
