"""Service registration for the HyperHDR integration.

Three device-targeted services (``set_color``, ``set_effect``, ``clear``),
registered once for the whole ``hyperhdr`` domain (not per config entry --
house pattern, see ha-vsphere's ``services.py``/ha-awtrix's ``__init__.py``)
and removed again once the last loaded config entry unloads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .entity import server_uid
from .exceptions import HyperHdrError

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

    from .client import HyperHdrInstanceClient
    from .coordinator import HyperHdrConfigEntry

SERVICE_SET_COLOR = "set_color"
SERVICE_SET_EFFECT = "set_effect"
SERVICE_CLEAR = "clear"

ATTR_DEVICE_ID = "device_id"
ATTR_RGB_COLOR = "rgb_color"
ATTR_EFFECT = "effect"
ATTR_PRIORITY = "priority"
ATTR_DURATION = "duration"

_BYTE = vol.All(vol.Coerce(int), vol.Range(min=0, max=255))

_SCHEMA_SET_COLOR = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): str,
        vol.Required(ATTR_RGB_COLOR): vol.All(vol.ExactSequence((_BYTE, _BYTE, _BYTE)), vol.Coerce(tuple)),
        vol.Optional(ATTR_PRIORITY): vol.Coerce(int),
        vol.Optional(ATTR_DURATION): vol.All(vol.Coerce(float), vol.Range(min=0)),
    }
)

_SCHEMA_SET_EFFECT = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): str,
        vol.Required(ATTR_EFFECT): str,
        vol.Optional(ATTR_PRIORITY): vol.Coerce(int),
        vol.Optional(ATTR_DURATION): vol.All(vol.Coerce(float), vol.Range(min=0)),
    }
)

_SCHEMA_CLEAR = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): str,
        # -1 clears every priority -- not optional (unlike set_color/
        # set_effect's priority) since silently defaulting to "clear
        # everything" (or to this integration's own default_priority,
        # which would just as silently NOT clear anything else) would be a
        # surprising default for a destructive action.
        vol.Required(ATTR_PRIORITY): vol.All(vol.Coerce(int), vol.Range(min=-1)),
    }
)


# ---------------------------------------------------------------------------
# Device resolver
# ---------------------------------------------------------------------------


def resolve_instance_target(
    hass: HomeAssistant, device_id: str
) -> tuple[HyperHdrConfigEntry, HyperHdrInstanceClient, int]:
    """Resolve a service call's ``device_id`` to ``(config_entry, instance_client, instance_id)``.

    This integration's device identifiers are either the server device
    ``(DOMAIN, server_uid)`` or an instance device ``(DOMAIN,
    f"{server_uid}_{instance_id}")`` -- see entity.py's
    ``server_device_info``/``instance_device_info``. Only an instance
    device is a valid target for these services (they all operate on one
    instance's command surface); every failure mode here (device not
    found, wrong integration, the server device itself, an instance that's
    currently stopped/disconnected) raises a ``ServiceValidationError``
    with an actionable message rather than an opaque lookup failure.
    """
    device_registry = dr.async_get(hass)
    device = device_registry.async_get(device_id)
    if device is None:
        raise ServiceValidationError(f"Device '{device_id}' not found")

    raw_id = next(
        (identifier for identifier_domain, identifier in device.identifiers if identifier_domain == DOMAIN), None
    )
    if raw_id is None:
        raise ServiceValidationError(f"Device '{device_id}' is not a HyperHDR device")

    for entry in hass.config_entries.async_entries(DOMAIN):
        if not hasattr(entry, "runtime_data"):
            continue
        uid = server_uid(entry)
        if raw_id == uid:
            raise ServiceValidationError("This service must target a HyperHDR instance device, not the server device")
        prefix = f"{uid}_"
        if not raw_id.startswith(prefix):
            continue
        try:
            instance_id = int(raw_id[len(prefix) :])
        except ValueError:
            continue
        coordinator = entry.runtime_data.instance_coordinators.get(instance_id)
        if coordinator is None or coordinator.client is None:
            raise ServiceValidationError(f"HyperHDR instance {instance_id} is not connected")
        return entry, coordinator.client, instance_id

    raise ServiceValidationError(f"Device '{device_id}' is not a connected HyperHDR instance")


def _duration_ms(seconds: float | None) -> int:
    """HyperHDR's ``duration`` fields are milliseconds; the service's field is seconds."""
    return 0 if seconds is None else round(seconds * 1000)


# ---------------------------------------------------------------------------
# Service handlers
# ---------------------------------------------------------------------------


async def _handle_set_color(call: ServiceCall) -> None:
    """Handle the ``set_color`` service call."""
    entry, client, _instance_id = resolve_instance_target(call.hass, call.data[ATTR_DEVICE_ID])
    priority = call.data.get(ATTR_PRIORITY, entry.runtime_data.default_priority)
    duration_ms = _duration_ms(call.data.get(ATTR_DURATION))
    rgb: tuple[int, int, int] = call.data[ATTR_RGB_COLOR]
    try:
        await client.async_set_color(rgb, priority, duration_ms)
    except HyperHdrError as err:
        raise HomeAssistantError(f"failed to set HyperHDR color: {err}") from err


async def _handle_set_effect(call: ServiceCall) -> None:
    """Handle the ``set_effect`` service call."""
    entry, client, _instance_id = resolve_instance_target(call.hass, call.data[ATTR_DEVICE_ID])
    priority = call.data.get(ATTR_PRIORITY, entry.runtime_data.default_priority)
    duration_ms = _duration_ms(call.data.get(ATTR_DURATION))
    effect: str = call.data[ATTR_EFFECT]
    try:
        await client.async_set_effect(effect, priority, duration_ms)
    except HyperHdrError as err:
        raise HomeAssistantError(f"failed to set HyperHDR effect {effect!r}: {err}") from err


async def _handle_clear(call: ServiceCall) -> None:
    """Handle the ``clear`` service call."""
    _entry, client, _instance_id = resolve_instance_target(call.hass, call.data[ATTR_DEVICE_ID])
    priority: int = call.data[ATTR_PRIORITY]
    try:
        await client.async_clear(priority)
    except HyperHdrError as err:
        raise HomeAssistantError(f"failed to clear HyperHDR priority {priority}: {err}") from err


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


def async_unload_services(hass: HomeAssistant) -> None:
    """Unregister HyperHDR services (idempotent). Called once the last
    loaded config entry for this domain unloads."""
    for service in (SERVICE_SET_COLOR, SERVICE_SET_EFFECT, SERVICE_CLEAR):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
