"""Tests for select.py: HDR mode mapping and priority_source option
building/current-value/selection parsing."""

from __future__ import annotations

import pytest
from conftest import FakeConfigEntry, FakeDomainClient, FakeHass
from homeassistant.exceptions import HomeAssistantError

from custom_components.hyperhdr.coordinator import (
    HyperHdrInstanceCoordinator,
    HyperHdrRuntimeData,
    HyperHdrServerCoordinator,
)
from custom_components.hyperhdr.exceptions import HyperHdrError
from custom_components.hyperhdr.models import (
    HyperHdrAdjustment,
    HyperHdrInstanceData,
    HyperHdrInstanceSummary,
    HyperHdrPriority,
    HyperHdrServerData,
    HyperHdrSysInfo,
)
from custom_components.hyperhdr.select import HyperHdrHdrToneMappingSelect, HyperHdrPrioritySourceSelect


def _adjustment() -> HyperHdrAdjustment:
    return HyperHdrAdjustment(
        luminance_gain=None,
        saturation_gain=None,
        backlight_threshold=None,
        gamma=None,
        temperature_red=None,
        temperature_green=None,
        temperature_blue=None,
    )


def _instance_data(**overrides: object) -> HyperHdrInstanceData:
    defaults: dict[str, object] = {
        "instance_id": 1,
        "components": {},
        "priorities": [],
        "priorities_autoselect": False,
        "adjustment": _adjustment(),
        "effects": [],
        "led_count": 0,
        "video_mode": "",
        "hdr_mode": 0,
        "connected": True,
    }
    defaults.update(overrides)
    return HyperHdrInstanceData(**defaults)  # type: ignore[arg-type]


def _priority(**overrides: object) -> HyperHdrPriority:
    defaults: dict[str, object] = {
        "priority": 128,
        "component_id": "COLOR",
        "origin": "Home Assistant",
        "owner": "",
        "active": True,
        "visible": True,
        "value": None,
    }
    defaults.update(overrides)
    return HyperHdrPriority(**defaults)  # type: ignore[arg-type]


def _make_setup() -> tuple[FakeConfigEntry, HyperHdrInstanceCoordinator, FakeDomainClient]:
    hass = FakeHass()
    entry = FakeConfigEntry(entry_id="entry1", unique_id="srv-uid", data={"host": "10.0.0.5", "port": 8090})
    server_coordinator = HyperHdrServerCoordinator(hass, entry, object())  # type: ignore[arg-type]
    server_coordinator.async_set_updated_data(
        HyperHdrServerData(
            sysinfo=HyperHdrSysInfo(id="dev", hostname="host", version="22", build="b"),
            instances={1: HyperHdrInstanceSummary(instance=1, friendly_name="Living Room", running=True)},
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
    coordinator = HyperHdrInstanceCoordinator(hass, entry, 1)
    entry.runtime_data.instance_coordinators[1] = coordinator
    client = FakeDomainClient()
    coordinator.client = client  # type: ignore[assignment]
    return entry, coordinator, client


class TestHdrToneMappingSelect:
    def test_current_option_off(self) -> None:
        entry, coordinator, _client = _make_setup()
        coordinator.async_set_updated_data(_instance_data(hdr_mode=0))
        select = HyperHdrHdrToneMappingSelect(coordinator, entry, 1)
        assert select.current_option == "off"

    def test_current_option_on(self) -> None:
        entry, coordinator, _client = _make_setup()
        coordinator.async_set_updated_data(_instance_data(hdr_mode=1))
        select = HyperHdrHdrToneMappingSelect(coordinator, entry, 1)
        assert select.current_option == "on"

    def test_current_option_none_for_unrecognized_mode(self) -> None:
        entry, coordinator, _client = _make_setup()
        coordinator.async_set_updated_data(_instance_data(hdr_mode=5))
        select = HyperHdrHdrToneMappingSelect(coordinator, entry, 1)
        assert select.current_option is None

    async def test_select_option_calls_set_hdr_mode(self) -> None:
        entry, coordinator, client = _make_setup()
        coordinator.async_set_updated_data(_instance_data())
        select = HyperHdrHdrToneMappingSelect(coordinator, entry, 1)
        await select.async_select_option("on")
        assert client.calls == [("async_set_hdr_mode", (1,), {})]

    async def test_wraps_hyperhdr_error(self) -> None:
        entry, coordinator, _client = _make_setup()
        coordinator.client = FakeDomainClient(raise_on=HyperHdrError("boom"))  # type: ignore[assignment]
        coordinator.async_set_updated_data(_instance_data())
        select = HyperHdrHdrToneMappingSelect(coordinator, entry, 1)
        with pytest.raises(HomeAssistantError):
            await select.async_select_option("on")


class TestPrioritySourceSelect:
    def test_options_from_active_priorities_only(self) -> None:
        entry, coordinator, _client = _make_setup()
        coordinator.async_set_updated_data(
            _instance_data(
                priorities=[
                    _priority(priority=200, component_id="COLOR", origin="Home Assistant", active=True),
                    _priority(priority=100, component_id="VIDEOGRABBER", origin="System", active=False),
                ]
            )
        )
        select = HyperHdrPrioritySourceSelect(coordinator, entry, 1)
        assert select.options == ["Auto", "200: COLOR (Home Assistant)"]

    def test_current_option_auto_when_autoselect(self) -> None:
        entry, coordinator, _client = _make_setup()
        coordinator.async_set_updated_data(_instance_data(priorities_autoselect=True))
        select = HyperHdrPrioritySourceSelect(coordinator, entry, 1)
        assert select.current_option == "Auto"

    def test_current_option_matches_visible_priority(self) -> None:
        entry, coordinator, _client = _make_setup()
        coordinator.async_set_updated_data(
            _instance_data(
                priorities_autoselect=False,
                priorities=[_priority(priority=200, component_id="COLOR", origin="Home Assistant", visible=True)],
            )
        )
        select = HyperHdrPrioritySourceSelect(coordinator, entry, 1)
        assert select.current_option == "200: COLOR (Home Assistant)"

    def test_current_option_none_when_nothing_visible_and_not_autoselect(self) -> None:
        entry, coordinator, _client = _make_setup()
        coordinator.async_set_updated_data(_instance_data(priorities_autoselect=False, priorities=[]))
        select = HyperHdrPrioritySourceSelect(coordinator, entry, 1)
        assert select.current_option is None

    async def test_select_auto_calls_select_source_none(self) -> None:
        entry, coordinator, client = _make_setup()
        coordinator.async_set_updated_data(_instance_data())
        select = HyperHdrPrioritySourceSelect(coordinator, entry, 1)
        await select.async_select_option("Auto")
        assert client.calls == [("async_select_source", (None,), {})]

    async def test_select_specific_option_parses_leading_int(self) -> None:
        entry, coordinator, client = _make_setup()
        coordinator.async_set_updated_data(_instance_data())
        select = HyperHdrPrioritySourceSelect(coordinator, entry, 1)
        await select.async_select_option("200: COLOR (Home Assistant)")
        assert client.calls == [("async_select_source", (200,), {})]

    async def test_wraps_hyperhdr_error(self) -> None:
        entry, coordinator, _client = _make_setup()
        coordinator.client = FakeDomainClient(raise_on=HyperHdrError("boom"))  # type: ignore[assignment]
        coordinator.async_set_updated_data(_instance_data())
        select = HyperHdrPrioritySourceSelect(coordinator, entry, 1)
        with pytest.raises(HomeAssistantError):
            await select.async_select_option("Auto")
