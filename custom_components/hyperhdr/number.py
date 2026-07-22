"""Number platform for the HyperHDR integration.

Per instance, all CONFIG, presence-based: a number entity is only created
for an adjustment field that's actually present in the live
``adjustment.raw`` payload (checked once, at entity-build time), so this
integration never claims to control a field a given HyperHDR build/config
doesn't expose.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory

from .const import SIGNAL_INSTANCE_READY
from .entity import HyperHdrInstanceEntity, require_instance_client, wait_for_connected_data
from .exceptions import HyperHdrError

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import HyperHdrConfigEntry, HyperHdrInstanceCoordinator
    from .models import HyperHdrAdjustment

# Fields whose native value must round-trip as an int (the client sends
# whatever Python value is passed straight through as JSON -- HyperHDR's own
# schema rejects a float where these expect an integer channel multiplier).
_INT_FIELDS = frozenset({"temperature_red", "temperature_green", "temperature_blue"})


@dataclass(frozen=True, kw_only=True)
class HyperHdrNumberDescription(NumberEntityDescription):
    """Describes a HyperHDR ``adjustment`` field as a number entity."""

    raw_key: str
    field_name: str
    value_fn: Callable[[HyperHdrAdjustment], float | int | None]


# Chosen ranges (see docs/api-notes.md + tests/fixtures/serverinfo_single_instance.json):
# - luminance_gain/gamma: HyperHDR's own UI sliders use these exact bounds
#   (0-1 for luminanceGain, 0.1-5 for gamma).
# - saturation_gain: no documented bound; the live fixture shows a neutral
#   value of 1 (matching luminanceGain's neutral-at-1 convention) and
#   HyperHDR/Hyperion-family adjustment sliders commonly cap saturation at
#   2x -- 0.0-2.0 chosen as a reasonable, symmetrical-around-neutral range.
# - backlight_threshold: observed live as a small fraction (~0.0039, i.e.
#   1/255), not a 0-100 percent despite the "threshold" name -- 0.0-1.0.
# - temperature_red/green/blue: per-channel gain multipliers, observed as
#   integer "1" (neutral) in serverinfo; 0-255 matches their role as an
#   8-bit channel multiplier.
NUMBERS: tuple[HyperHdrNumberDescription, ...] = (
    HyperHdrNumberDescription(
        key="luminance_gain",
        name="Luminance gain",
        raw_key="luminanceGain",
        field_name="luminance_gain",
        native_min_value=0.0,
        native_max_value=1.0,
        native_step=0.01,
        value_fn=lambda adjustment: adjustment.luminance_gain,
    ),
    HyperHdrNumberDescription(
        key="saturation_gain",
        name="Saturation gain",
        raw_key="saturationGain",
        field_name="saturation_gain",
        native_min_value=0.0,
        native_max_value=2.0,
        native_step=0.01,
        value_fn=lambda adjustment: adjustment.saturation_gain,
    ),
    HyperHdrNumberDescription(
        key="gamma",
        name="Gamma",
        raw_key="gamma",
        field_name="gamma",
        native_min_value=0.1,
        native_max_value=5.0,
        native_step=0.05,
        value_fn=lambda adjustment: adjustment.gamma,
    ),
    HyperHdrNumberDescription(
        key="backlight_threshold",
        name="Backlight threshold",
        raw_key="backlightThreshold",
        field_name="backlight_threshold",
        native_min_value=0.0,
        native_max_value=1.0,
        native_step=0.001,
        value_fn=lambda adjustment: adjustment.backlight_threshold,
    ),
    HyperHdrNumberDescription(
        key="temperature_red",
        name="Temperature red",
        raw_key="temperatureRed",
        field_name="temperature_red",
        native_min_value=0,
        native_max_value=255,
        native_step=1,
        value_fn=lambda adjustment: adjustment.temperature_red,
    ),
    HyperHdrNumberDescription(
        key="temperature_green",
        name="Temperature green",
        raw_key="temperatureGreen",
        field_name="temperature_green",
        native_min_value=0,
        native_max_value=255,
        native_step=1,
        value_fn=lambda adjustment: adjustment.temperature_green,
    ),
    HyperHdrNumberDescription(
        key="temperature_blue",
        name="Temperature blue",
        raw_key="temperatureBlue",
        field_name="temperature_blue",
        native_min_value=0,
        native_max_value=255,
        native_step=1,
        value_fn=lambda adjustment: adjustment.temperature_blue,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: HyperHdrConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up HyperHDR number entities."""
    runtime = entry.runtime_data
    entity_lists = await asyncio.gather(
        *(
            _entities_for_instance(entry, coordinator, instance_id)
            for instance_id, coordinator in runtime.instance_coordinators.items()
        )
    )
    entities: list[HyperHdrAdjustmentNumber] = [number for sublist in entity_lists for number in sublist]
    async_add_entities(entities)

    async def _add_for_instance(instance_id: int) -> None:
        coordinator = runtime.instance_coordinators[instance_id]
        async_add_entities(await _entities_for_instance(entry, coordinator, instance_id))

    entry.async_on_unload(
        async_dispatcher_connect(hass, f"{SIGNAL_INSTANCE_READY}_{entry.entry_id}", _add_for_instance)
    )


async def _entities_for_instance(
    entry: HyperHdrConfigEntry, coordinator: HyperHdrInstanceCoordinator, instance_id: int
) -> list[HyperHdrAdjustmentNumber]:
    """Build one number entity per adjustment field present in the live data.

    Bounded-waits for a connected snapshot first (``wait_for_connected_data``)
    -- see switch.py's identical rationale on ``_component_entities_for_instance``:
    ``coordinator.data.adjustment.raw`` is an empty placeholder until the
    instance's client finishes connecting, and this presence check is a
    one-time snapshot deciding which entities get created at all.
    """
    data = await wait_for_connected_data(coordinator)
    raw = data.adjustment.raw
    return [
        HyperHdrAdjustmentNumber(coordinator, entry, instance_id, description)
        for description in NUMBERS
        if description.raw_key in raw
    ]


class HyperHdrAdjustmentNumber(HyperHdrInstanceEntity, NumberEntity):
    """One writable field of ``data.adjustment``."""

    entity_description: HyperHdrNumberDescription
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: HyperHdrInstanceCoordinator,
        entry: HyperHdrConfigEntry,
        instance_id: int,
        description: HyperHdrNumberDescription,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator, entry, instance_id, description.key)
        self.entity_description = description
        self._attr_native_min_value = description.native_min_value
        self._attr_native_max_value = description.native_max_value
        self._attr_native_step = description.native_step

    @property
    def native_value(self) -> float | int | None:
        """The field's current value, from the typed adjustment snapshot."""
        return self.entity_description.value_fn(self.coordinator.data.adjustment)

    async def async_set_native_value(self, value: float) -> None:
        """Push a single-field adjustment update."""
        client = require_instance_client(self.coordinator)
        field_name = self.entity_description.field_name
        payload_value: float | int = round(value) if field_name in _INT_FIELDS else value
        try:
            await client.async_set_adjustment(**{field_name: payload_value})
        except HyperHdrError as err:
            raise HomeAssistantError(f"failed to set HyperHDR {field_name}: {err}") from err
