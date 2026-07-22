"""Diagnostics support for the HyperHDR integration.

Redacts anything that could identify or grant access to the physical
server: the API token, admin password, the config entry's own ``host``/
``unique_id``, and ``sysinfo``'s ``id`` field -- which IS the same value as
``unique_id`` (see ``entity.py``'s ``server_uid``), just surfaced a second
time inside the payload built below.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import HyperHdrConfigEntry, HyperHdrInstanceCoordinator

TO_REDACT = {"token", "admin_password", "password", "host", "unique_id", "id"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: HyperHdrConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    server_data = runtime.server_coordinator.data

    payload: dict[str, Any] = {
        "entry": entry.as_dict(),
        "server": {
            "sysinfo": asdict(server_data.sysinfo) if server_data is not None else {},
            "connected": server_data.connected if server_data is not None else False,
            "roster": {
                str(instance_id): {"friendly_name": summary.friendly_name, "running": summary.running}
                for instance_id, summary in (server_data.instances if server_data is not None else {}).items()
            },
            "default_priority": runtime.default_priority,
            "hidden_effects": sorted(runtime.hidden_effects),
        },
        "instances": {
            str(instance_id): _instance_diagnostics(coordinator)
            for instance_id, coordinator in runtime.instance_coordinators.items()
        },
    }
    # `homeassistant` is unavailable to mypy in this dev environment
    # (ignore_missing_imports=True treats it as Any) -- the explicit
    # annotation here (rather than returning the call directly) is what
    # keeps the `-> dict[str, Any]` return type actually checked.
    redacted: dict[str, Any] = async_redact_data(payload, TO_REDACT)
    return redacted


def _instance_diagnostics(coordinator: HyperHdrInstanceCoordinator) -> dict[str, Any]:
    """One instance's diagnostics dict, from its coordinator snapshot + attached client."""
    data = coordinator.data
    client = coordinator.client
    return {
        "connected": data.connected if data is not None else False,
        "components": {name: component.enabled for name, component in data.components.items()}
        if data is not None
        else {},
        "adjustment_raw": data.adjustment.raw if data is not None else {},
        "priorities": [asdict(priority) for priority in data.priorities] if data is not None else [],
        "priorities_autoselect": data.priorities_autoselect if data is not None else False,
        "effects_count": len(data.effects) if data is not None else 0,
        "led_count": data.led_count if data is not None else 0,
        "video_mode": data.video_mode if data is not None else "",
        "hdr_mode": data.hdr_mode if data is not None else None,
        "client": {
            "connected": client.connected if client is not None else False,
            "admin_logged_in": bool(client.admin_logged_in) if client is not None else False,
            "token_required": bool(client.token_required) if client is not None else False,
            "malformed_or_unmatched_count": client.malformed_or_unmatched_count if client is not None else 0,
        },
    }
