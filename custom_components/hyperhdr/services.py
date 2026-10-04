"""Service registration for the HyperHDR integration.

Three target-based services (standard device/entity/area ``target``) (``set_color``, ``set_effect``, ``clear``),
registered once for the whole ``hyperhdr`` domain (not per config entry --
house pattern, see ha-vsphere's ``services.py``/ha-awtrix's ``__init__.py``)
and removed again once the last loaded config entry unloads.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids

from .const import DOMAIN
from .entity import server_uid
from .exceptions import HyperHdrError

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

    from .client import HyperHdrInstanceClient
    from .coordinator import HyperHdrConfigEntry

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_COLOR = "set_color"
SERVICE_SET_EFFECT = "set_effect"
SERVICE_CLEAR = "clear"

ATTR_RGB_COLOR = "rgb_color"
ATTR_EFFECT = "effect"
ATTR_PRIORITY = "priority"
ATTR_DURATION = "duration"

_BYTE = vol.All(vol.Coerce(int), vol.Range(min=0, max=255))

# ``cv.ENTITY_SERVICE_FIELDS`` is HA's standard target-key set (entity_id,
# device_id, area_id, floor_id, label_id), all optional.
_SCHEMA_SET_COLOR = vol.Schema(
    {
        **cv.ENTITY_SERVICE_FIELDS,
        vol.Required(ATTR_RGB_COLOR): vol.All(vol.ExactSequence((_BYTE, _BYTE, _BYTE)), vol.Coerce(tuple)),
        vol.Optional(ATTR_PRIORITY): vol.Coerce(int),
        vol.Optional(ATTR_DURATION): vol.All(vol.Coerce(float), vol.Range(min=0)),
    }
)

_SCHEMA_SET_EFFECT = vol.Schema(
    {
        **cv.ENTITY_SERVICE_FIELDS,
        vol.Required(ATTR_EFFECT): str,
        vol.Optional(ATTR_PRIORITY): vol.Coerce(int),
        vol.Optional(ATTR_DURATION): vol.All(vol.Coerce(float), vol.Range(min=0)),
    }
)

_SCHEMA_CLEAR = vol.Schema(
    {
        **cv.ENTITY_SERVICE_FIELDS,
        # -1 clears every priority -- not optional (unlike set_color/
        # set_effect's priority) since silently defaulting to "clear
        # everything" (or to this integration's own default_priority,
        # which would just as silently NOT clear anything else) would be a
        # surprising default for a destructive action.
        vol.Required(ATTR_PRIORITY): vol.All(vol.Coerce(int), vol.Range(min=-1)),
    }
)

type _Target = tuple[HyperHdrConfigEntry, HyperHdrInstanceClient, int]


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


def _resolve_device(hass: HomeAssistant, device_id: str, *, strict: bool = True) -> _Target | None:
    """Resolve a device id to ``(config_entry, instance_client, instance_id)``.

    This integration's device identifiers are either the server device
    ``(DOMAIN, server_uid)`` or an instance device ``(DOMAIN,
    f"{server_uid}_{instance_id}")`` -- see entity.py's
    ``server_device_info``/``instance_device_info``. Only an instance
    device is a valid target. With ``strict`` (a device the caller named
    directly) every failure mode raises a ``ServiceValidationError``; when
    not strict (a device merely inferred from an entity/area target) a
    device that is unknown, foreign or the server device returns ``None``
    so it is skipped. A stopped/disconnected instance raises when strict and is skipped otherwise.
    """
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        if strict:
            raise ServiceValidationError(f"Device '{device_id}' not found")
        return None

    raw_id = next(
        (identifier for identifier_domain, identifier in device.identifiers if identifier_domain == DOMAIN), None
    )
    if raw_id is None:
        if strict:
            raise ServiceValidationError(f"Device '{device_id}' is not a HyperHDR device")
        return None

    for entry in hass.config_entries.async_entries(DOMAIN):
        if not hasattr(entry, "runtime_data"):
            continue
        uid = server_uid(entry)
        if raw_id == uid:
            if strict:
                raise ServiceValidationError(
                    "This service must target a HyperHDR instance device, not the server device"
                )
            return None
        prefix = f"{uid}_"
        if not raw_id.startswith(prefix):
            continue
        try:
            instance_id = int(raw_id[len(prefix) :])
        except ValueError:
            continue
        coordinator = entry.runtime_data.instance_coordinators.get(instance_id)
        if coordinator is None or coordinator.client is None:
            if not strict:
                _LOGGER.debug("Skipping HyperHDR instance %s: not connected", instance_id)
                return None
            raise ServiceValidationError(f"HyperHDR instance {instance_id} is not connected")
        return entry, coordinator.client, instance_id

    if strict:
        raise ServiceValidationError(f"Device '{device_id}' is not a connected HyperHDR instance")
    return None


async def resolve_targets(hass: HomeAssistant, call: ServiceCall) -> list[_Target]:
    """Resolve a call's standard ``target`` (device/entity/area/...) to unique instances."""
    target_selection = TargetSelection(call.data)
    selected = async_extract_referenced_entity_ids(hass, target_selection)
    entity_registry = er.async_get(hass)

    targets: dict[tuple[str, int], _Target] = {}

    def _add(target: _Target | None) -> None:
        if target is not None:
            targets.setdefault((target[0].entry_id, target[2]), target)

    # Only devices named directly are strict; devices that merely came from
    # an area/floor/label/entity expansion are skipped when not an instance.
    for device_id in target_selection.device_ids:
        _add(_resolve_device(hass, device_id))

    inferred_devices = set(selected.referenced_devices)
    for entity_id in selected.referenced | selected.indirectly_referenced:
        reg_entry = entity_registry.async_get(entity_id)
        if reg_entry is not None and reg_entry.device_id:
            inferred_devices.add(reg_entry.device_id)
    for device_id in inferred_devices - target_selection.device_ids:
        _add(_resolve_device(hass, device_id, strict=False))

    if not targets:
        raise ServiceValidationError("No HyperHDR instance targeted")
    return list(targets.values())


def _duration_ms(seconds: float | None) -> int:
    """HyperHDR's ``duration`` fields are milliseconds; the service's field is seconds."""
    return 0 if seconds is None else round(seconds * 1000)


# ---------------------------------------------------------------------------
# Service handlers
# ---------------------------------------------------------------------------


async def _handle_set_color(call: ServiceCall) -> None:
    """Handle the ``set_color`` service call."""
    duration_ms = _duration_ms(call.data.get(ATTR_DURATION))
    rgb: tuple[int, int, int] = call.data[ATTR_RGB_COLOR]

    async def _run(target: _Target) -> None:
        entry, client, _instance_id = target
        priority = call.data.get(ATTR_PRIORITY, entry.runtime_data.default_priority)
        try:
            await client.async_set_color(rgb, priority, duration_ms)
        except HyperHdrError as err:
            raise HomeAssistantError(f"failed to set HyperHDR color: {err}") from err

    await asyncio.gather(*(_run(t) for t in await resolve_targets(call.hass, call)))


async def _handle_set_effect(call: ServiceCall) -> None:
    """Handle the ``set_effect`` service call."""
    duration_ms = _duration_ms(call.data.get(ATTR_DURATION))
    effect: str = call.data[ATTR_EFFECT]

    async def _run(target: _Target) -> None:
        entry, client, _instance_id = target
        priority = call.data.get(ATTR_PRIORITY, entry.runtime_data.default_priority)
        try:
            await client.async_set_effect(effect, priority, duration_ms)
        except HyperHdrError as err:
            raise HomeAssistantError(f"failed to set HyperHDR effect {effect!r}: {err}") from err

    await asyncio.gather(*(_run(t) for t in await resolve_targets(call.hass, call)))


async def _handle_clear(call: ServiceCall) -> None:
    """Handle the ``clear`` service call."""
    priority: int = call.data[ATTR_PRIORITY]

    async def _run(target: _Target) -> None:
        try:
            await target[1].async_clear(priority)
        except HyperHdrError as err:
            raise HomeAssistantError(f"failed to clear HyperHDR priority {priority}: {err}") from err

    await asyncio.gather(*(_run(t) for t in await resolve_targets(call.hass, call)))


# ---------------------------------------------------------------------------
# Registration / unregistration
# ---------------------------------------------------------------------------


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register HyperHDR services once for the whole domain (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_COLOR):
        return
    hass.services.async_register(DOMAIN, SERVICE_SET_COLOR, _handle_set_color, schema=_SCHEMA_SET_COLOR)
    hass.services.async_register(DOMAIN, SERVICE_SET_EFFECT, _handle_set_effect, schema=_SCHEMA_SET_EFFECT)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR, _handle_clear, schema=_SCHEMA_CLEAR)
