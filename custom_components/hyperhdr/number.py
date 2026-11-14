"""Number platform for the HyperHDR integration.

Per instance, all CONFIG, presence-based: a number entity is only created
for an adjustment field that's actually present in the live
``adjustment.raw`` payload (checked once, at entity-build time), so this
integration never claims to control a field a given HyperHDR build/config
doesn't expose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.helpers.entity import EntityCategory

from .entity import HyperHdrInstanceEntity, async_setup_connected_entities, require_instance_client
from .exceptions import HyperHdrError, translated_error

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import HyperHdrConfigEntry, HyperHdrInstanceCoordinator
    from .models import HyperHdrAdjustment, HyperHdrInstanceData

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
        translation_key="luminance_gain",
        raw_key="luminanceGain",
        field_name="luminance_gain",
        native_min_value=0.0,
        native_max_value=1.0,
        native_step=0.01,
        value_fn=lambda adjustment: adjustment.luminance_gain,
    ),
    HyperHdrNumberDescription(
        key="saturation_gain",
        translation_key="saturation_gain",
        raw_key="saturationGain",
        field_name="saturation_gain",
        native_min_value=0.0,
        native_max_value=2.0,
        native_step=0.01,
        value_fn=lambda adjustment: adjustment.saturation_gain,
    ),
    HyperHdrNumberDescription(
        key="gamma",
        translation_key="gamma",
        raw_key="gamma",
        field_name="gamma",
        native_min_value=0.1,
        native_max_value=5.0,
        native_step=0.05,
        value_fn=lambda adjustment: adjustment.gamma,
    ),
    HyperHdrNumberDescription(
        key="backlight_threshold",
        translation_key="backlight_threshold",
        raw_key="backlightThreshold",
        field_name="backlight_threshold",
        native_min_value=0.0,
        native_max_value=1.0,
        native_step=0.001,
        value_fn=lambda adjustment: adjustment.backlight_threshold,
    ),
    HyperHdrNumberDescription(
        key="temperature_red",
        translation_key="temperature_red",
        raw_key="temperatureRed",
        field_name="temperature_red",
        native_min_value=0,
        native_max_value=255,
        native_step=1,
        value_fn=lambda adjustment: adjustment.temperature_red,
    ),
    HyperHdrNumberDescription(
        key="temperature_green",
        translation_key="temperature_green",
        raw_key="temperatureGreen",
        field_name="temperature_green",
        native_min_value=0,
        native_max_value=255,
        native_step=1,
        value_fn=lambda adjustment: adjustment.temperature_green,
    ),
    HyperHdrNumberDescription(
        key="temperature_blue",
        translation_key="temperature_blue",
        raw_key="temperatureBlue",
        field_name="temperature_blue",
        native_min_value=0,
        native_max_value=255,
        native_step=1,
        value_fn=lambda adjustment: adjustment.temperature_blue,
    ),
)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: HyperHdrConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up HyperHDR number entities."""
    await async_setup_connected_entities(hass, entry, async_add_entities, _entities_for_data)


def _entities_for_data(
    entry: HyperHdrConfigEntry, data: HyperHdrInstanceData, coordinator: HyperHdrInstanceCoordinator, instance_id: int
) -> list[HyperHdrAdjustmentNumber]:
    """One number entity per adjustment field present in a *connected* snapshot.

    ``coordinator.data.adjustment.raw`` is an empty placeholder until the
    instance's client finishes connecting; ``async_setup_connected_entities``
    only calls this with connected data (and again on later connected
    updates, deduping by unique_id, so fields from a late snapshot still
    get their entity).
    """
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
            raise translated_error("adjustment_failed", field=field_name, error=err) from err
