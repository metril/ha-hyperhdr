"""Light platform for the HyperHDR integration.

One ``HyperHdrLight`` per instance -- the device's primary entity
(``_attr_name = None``), mapping HyperHDR's LEDDEVICE component, the visible
COLOR/EFFECT priority, and ``adjustment.luminanceGain`` onto HA's RGB light
model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import (
    COMPONENT_LEDDEVICE,
    EFFECT_SOLID,
    PRIORITY_COMPONENT_COLOR,
    PRIORITY_COMPONENT_EFFECT,
    SIGNAL_INSTANCE_READY,
)
from .entity import HyperHdrInstanceEntity, require_instance_client
from .exceptions import HyperHdrError
from .models import brightness_to_luminance_gain, luminance_gain_to_brightness

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import HyperHdrConfigEntry, HyperHdrInstanceCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: HyperHdrConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up HyperHDR light entities: one per existing/newly-ready instance."""
    runtime = entry.runtime_data
    entities: list[HyperHdrLight] = []
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
) -> list[HyperHdrLight]:
    return [HyperHdrLight(coordinator, entry, instance_id)]


class HyperHdrLight(HyperHdrInstanceEntity, LightEntity):
    """The primary light entity for one HyperHDR instance."""

    _attr_name = None
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_color_mode = ColorMode.RGB
    _attr_supported_features = LightEntityFeature.EFFECT

    def __init__(self, coordinator: HyperHdrInstanceCoordinator, entry: HyperHdrConfigEntry, instance_id: int) -> None:
        """Initialize the light."""
        super().__init__(coordinator, entry, instance_id, "light")
        self._default_priority = entry.runtime_data.default_priority
        self._hidden_effects = entry.runtime_data.hidden_effects
        # HyperHDR reports no "last set color" once an effect/other source
        # takes over the visible priority -- cached here so rgb_color still
        # shows something sensible (and Solid has a color to fall back to)
        # instead of going None the moment a COLOR priority stops being
        # visible.
        self._last_color: tuple[int, int, int] | None = None

    @property
    def is_on(self) -> bool:
        """Whether the LEDDEVICE component is enabled."""
        component = self.coordinator.data.components.get(COMPONENT_LEDDEVICE)
        return component.enabled if component is not None else False

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """The visible COLOR priority's RGB, or the last color we set."""
        for priority in self.coordinator.data.priorities:
            if priority.visible and priority.component_id == PRIORITY_COMPONENT_COLOR:
                rgb: tuple[int, int, int] | None = priority.rgb
                if rgb is not None:
                    self._last_color = rgb
                    return rgb
        return self._last_color

    @property
    def brightness(self) -> int | None:
        """Brightness mapped from ``adjustment.luminanceGain``, None-safe."""
        gain = self.coordinator.data.adjustment.luminance_gain
        if gain is None:
            return None
        return luminance_gain_to_brightness(gain)

    @property
    def effect_list(self) -> list[str]:
        """``Solid`` plus every known effect not hidden via options."""
        return [EFFECT_SOLID] + [
            effect.name for effect in self.coordinator.data.effects if effect.name not in self._hidden_effects
        ]

    @property
    def effect(self) -> str | None:
        """The visible EFFECT priority's owner, ``Solid`` for a visible
        COLOR priority, or None for anything else (including nothing
        visible at all)."""
        for priority in self.coordinator.data.priorities:
            if not priority.visible:
                continue
            if priority.component_id == PRIORITY_COMPONENT_EFFECT:
                return priority.owner or None
            if priority.component_id == PRIORITY_COMPONENT_COLOR:
                return EFFECT_SOLID
            return None
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the light, applying whichever kwargs HA passed."""
        client = require_instance_client(self.coordinator)
        try:
            if not self.is_on:
                await client.async_set_component(COMPONENT_LEDDEVICE, True)

            if ATTR_BRIGHTNESS in kwargs:
                gain = brightness_to_luminance_gain(kwargs[ATTR_BRIGHTNESS])
                await client.async_set_adjustment(luminance_gain=gain)

            effect = kwargs.get(ATTR_EFFECT)
            if effect is not None and effect != EFFECT_SOLID:
                await client.async_set_effect(effect, self._default_priority)
            elif effect == EFFECT_SOLID:
                await client.async_set_color(self._last_color or (255, 255, 255), self._default_priority)

            if ATTR_RGB_COLOR in kwargs:
                await client.async_set_color(kwargs[ATTR_RGB_COLOR], self._default_priority)
        except HyperHdrError as err:
            raise HomeAssistantError(f"failed to turn on HyperHDR light: {err}") from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light by disabling the LEDDEVICE component."""
        client = require_instance_client(self.coordinator)
        try:
            await client.async_set_component(COMPONENT_LEDDEVICE, False)
        except HyperHdrError as err:
            raise HomeAssistantError(f"failed to turn off HyperHDR light: {err}") from err
