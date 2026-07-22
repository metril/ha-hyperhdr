"""Tests for diagnostics.py: no secret strings survive redaction, and the
non-sensitive fields (components/priorities/effects count/roster/counters)
still come through."""

from __future__ import annotations

import json
from typing import Any

from conftest import FakeConfigEntry, FakeHass

from custom_components.hyperhdr.coordinator import (
    HyperHdrInstanceCoordinator,
    HyperHdrRuntimeData,
    HyperHdrServerCoordinator,
)
from custom_components.hyperhdr.diagnostics import async_get_config_entry_diagnostics
from custom_components.hyperhdr.models import (
    HyperHdrAdjustment,
    HyperHdrComponent,
    HyperHdrInstanceData,
    HyperHdrInstanceSummary,
    HyperHdrPriority,
    HyperHdrServerData,
    HyperHdrSysInfo,
)

_HOST = "10.20.30.40"
_TOKEN = "super-secret-token"  # noqa: S105
_ADMIN_PASSWORD = "hunter2"  # noqa: S105
_SYSINFO_ID = "hyperhdr-device-uid-1234"


class _FakeInstanceClient:
    """Just the attributes diagnostics.py reads off an attached client."""

    def __init__(self) -> None:
        self.connected = True
        self.admin_logged_in = True
        self.token_required = False
        self.malformed_or_unmatched_count = 3


def _adjustment() -> HyperHdrAdjustment:
    return HyperHdrAdjustment(
        luminance_gain=0.5,
        saturation_gain=None,
        backlight_threshold=None,
        gamma=None,
        temperature_red=None,
        temperature_green=None,
        temperature_blue=None,
        raw={"luminanceGain": 0.5},
    )


def _make_entry_with_runtime_data() -> FakeConfigEntry:
    hass = FakeHass()
    entry = FakeConfigEntry(
        entry_id="entry1",
        unique_id=_SYSINFO_ID,
        data={
            "host": _HOST,
            "port": 8090,
            "token": _TOKEN,
            "admin_password": _ADMIN_PASSWORD,
        },
        options={"default_priority": 128},
    )

    server_coordinator = HyperHdrServerCoordinator(hass, entry, object())  # type: ignore[arg-type]
    server_coordinator.async_set_updated_data(
        HyperHdrServerData(
            sysinfo=HyperHdrSysInfo(id=_SYSINFO_ID, hostname="living-room-hyperhdr", version="22.0.0", build="b1"),
            instances={0: HyperHdrInstanceSummary(instance=0, friendly_name="Living Room", running=True)},
            connected=True,
        )
    )

    entry.runtime_data = HyperHdrRuntimeData(  # type: ignore[attr-defined]
        server_client=object(),  # type: ignore[arg-type]
        server_coordinator=server_coordinator,
        instance_coordinators={},
        default_priority=128,
        hidden_effects=set(),
    )

    instance_coordinator = HyperHdrInstanceCoordinator(hass, entry, 0)
    instance_coordinator.async_set_updated_data(
        HyperHdrInstanceData(
            instance_id=0,
            components={"LEDDEVICE": HyperHdrComponent(name="LEDDEVICE", enabled=True)},
            priorities=[
                HyperHdrPriority(
                    priority=128,
                    component_id="COLOR",
                    origin="Home Assistant",
                    owner="",
                    active=True,
                    visible=True,
                    value={"RGB": [255, 0, 255]},
                )
            ],
            priorities_autoselect=False,
            adjustment=_adjustment(),
            effects=[],
            led_count=1,
            video_mode="",
            hdr_mode=1,
            connected=True,
        )
    )
    instance_coordinator.client = _FakeInstanceClient()  # type: ignore[assignment]
    entry.runtime_data.instance_coordinators[0] = instance_coordinator

    return entry


def _find_secret_strings(value: Any) -> list[str]:
    """Recursively collect every occurrence (anywhere: key or value) of a
    known secret literal, as a serialized-JSON substring search -- the most
    reliable way to prove "no token/password/host anywhere in output",
    matching how the live-validation checklist greps the real endpoint's
    response body."""
    haystack = json.dumps(value)
    return [secret for secret in (_TOKEN, _ADMIN_PASSWORD, _HOST, _SYSINFO_ID) if secret in haystack]


class TestAsyncGetConfigEntryDiagnostics:
    async def test_no_secret_or_identifying_strings_survive_redaction(self) -> None:
        hass = FakeHass()
        entry = _make_entry_with_runtime_data()

        diagnostics = await async_get_config_entry_diagnostics(hass, entry)  # type: ignore[arg-type]

        assert _find_secret_strings(diagnostics) == []

    async def test_redacted_keys_show_the_redaction_marker(self) -> None:
        hass = FakeHass()
        entry = _make_entry_with_runtime_data()

        diagnostics = await async_get_config_entry_diagnostics(hass, entry)  # type: ignore[arg-type]

        assert diagnostics["entry"]["data"]["token"] == "**REDACTED**"
        assert diagnostics["entry"]["data"]["admin_password"] == "**REDACTED**"
        assert diagnostics["entry"]["data"]["host"] == "**REDACTED**"
        assert diagnostics["entry"]["unique_id"] == "**REDACTED**"
        assert diagnostics["server"]["sysinfo"]["id"] == "**REDACTED**"

    async def test_non_sensitive_fields_pass_through(self) -> None:
        hass = FakeHass()
        entry = _make_entry_with_runtime_data()

        diagnostics = await async_get_config_entry_diagnostics(hass, entry)  # type: ignore[arg-type]

        instance = diagnostics["instances"]["0"]
        assert instance["connected"] is True
        assert instance["components"] == {"LEDDEVICE": True}
        assert instance["adjustment_raw"] == {"luminanceGain": 0.5}
        assert instance["effects_count"] == 0
        assert instance["hdr_mode"] == 1
        assert instance["led_count"] == 1
        assert instance["client"]["admin_logged_in"] is True
        assert instance["client"]["malformed_or_unmatched_count"] == 3
        # A COLOR priority's RGB value is not redacted -- see TO_REDACT.
        assert instance["priorities"][0]["value"] == {"RGB": [255, 0, 255]}
        assert diagnostics["server"]["roster"]["0"]["friendly_name"] == "Living Room"
        assert diagnostics["server"]["sysinfo"]["hostname"] == "living-room-hyperhdr"

    async def test_disconnected_instance_with_no_client_does_not_raise(self) -> None:
        hass = FakeHass()
        entry = _make_entry_with_runtime_data()
        entry.runtime_data.instance_coordinators[0].client = None  # type: ignore[attr-defined]

        diagnostics = await async_get_config_entry_diagnostics(hass, entry)  # type: ignore[arg-type]

        assert diagnostics["instances"]["0"]["client"]["connected"] is False
