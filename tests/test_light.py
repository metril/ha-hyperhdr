"""Tests for light.py: state mapping and turn_on/turn_off command selection.

Pure-logic focus per the Phase 5+6 brief -- entities are built directly
against a real ``HyperHdrInstanceCoordinator`` seeded with hand-built
``HyperHdrInstanceData`` snapshots, with a recording ``FakeDomainClient``
standing in for the wire client. No dispatcher/async_setup_entry wiring is
exercised here (that's the live-HA validation's job).
"""

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
from custom_components.hyperhdr.light import HyperHdrLight
from custom_components.hyperhdr.models import (
    HyperHdrAdjustment,
    HyperHdrComponent,
    HyperHdrEffect,
    HyperHdrInstanceData,
    HyperHdrInstanceSummary,
    HyperHdrPriority,
    HyperHdrServerData,
    HyperHdrSysInfo,
    luminance_gain_to_brightness,
)


def _adjustment(**overrides: object) -> HyperHdrAdjustment:
    defaults: dict[str, object] = {
        "luminance_gain": None,
        "saturation_gain": None,
        "backlight_threshold": None,
        "gamma": None,
        "temperature_red": None,
        "temperature_green": None,
        "temperature_blue": None,
    }
    defaults.update(overrides)
    return HyperHdrAdjustment(**defaults)  # type: ignore[arg-type]


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
        "component_id": "",
        "origin": "",
        "owner": "",
        "active": True,
        "visible": True,
        "value": None,
    }
    defaults.update(overrides)
    return HyperHdrPriority(**defaults)  # type: ignore[arg-type]


def _make_light(
    *, default_priority: int = 128, hidden_effects: set[str] | None = None, client: FakeDomainClient | None = None
) -> tuple[HyperHdrLight, HyperHdrInstanceCoordinator, FakeDomainClient]:
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
        default_priority=default_priority,
        hidden_effects=hidden_effects or set(),
    )
    coordinator = HyperHdrInstanceCoordinator(hass, entry, 1)
    entry.runtime_data.instance_coordinators[1] = coordinator
    fake_client = client if client is not None else FakeDomainClient()
    coordinator.client = fake_client  # type: ignore[assignment]
    light = HyperHdrLight(coordinator, entry, 1)
    return light, coordinator, fake_client


class TestUniqueIdAndStatics:
    def test_unique_id_and_statics(self) -> None:
        light, _coordinator, _client = _make_light()
        assert light._attr_unique_id == "srv-uid_1_light"
        assert light._attr_name is None


class TestIsOn:
    def test_on_when_leddevice_enabled(self) -> None:
        light, coordinator, _client = _make_light()
        coordinator.async_set_updated_data(
            _instance_data(components={"LEDDEVICE": HyperHdrComponent(name="LEDDEVICE", enabled=True)})
        )
        assert light.is_on is True

    def test_off_when_leddevice_disabled(self) -> None:
        light, coordinator, _client = _make_light()
        coordinator.async_set_updated_data(
            _instance_data(components={"LEDDEVICE": HyperHdrComponent(name="LEDDEVICE", enabled=False)})
        )
        assert light.is_on is False

    def test_off_when_leddevice_absent(self) -> None:
        light, coordinator, _client = _make_light()
        coordinator.async_set_updated_data(_instance_data(components={}))
        assert light.is_on is False


class TestRgbColor:
    def test_from_visible_color_priority(self) -> None:
        light, coordinator, _client = _make_light()
        coordinator.async_set_updated_data(
            _instance_data(
                priorities=[
                    _priority(component_id="COLOR", visible=True, value={"RGB": [10, 20, 30]}),
                ]
            )
        )
        assert light.rgb_color == (10, 20, 30)

    def test_ignores_non_visible_or_non_color_priorities(self) -> None:
        light, coordinator, _client = _make_light()
        coordinator.async_set_updated_data(
            _instance_data(
                priorities=[
                    _priority(component_id="COLOR", visible=False, value={"RGB": [1, 2, 3]}),
                    _priority(component_id="VIDEOGRABBER", visible=True, value=None),
                ]
            )
        )
        assert light.rgb_color is None

    def test_falls_back_to_last_set_color_once_priority_no_longer_visible(self) -> None:
        light, coordinator, _client = _make_light()
        coordinator.async_set_updated_data(
            _instance_data(priorities=[_priority(component_id="COLOR", visible=True, value={"RGB": [9, 9, 9]})])
        )
        assert light.rgb_color == (9, 9, 9)
        # An effect takes over -- no COLOR priority visible anymore.
        coordinator.async_set_updated_data(
            _instance_data(priorities=[_priority(component_id="EFFECT", visible=True, owner="Rainbow")])
        )
        assert light.rgb_color == (9, 9, 9)


class TestBrightness:
    def test_maps_luminance_gain(self) -> None:
        light, coordinator, _client = _make_light()
        coordinator.async_set_updated_data(_instance_data(adjustment=_adjustment(luminance_gain=0.5)))
        assert light.brightness == luminance_gain_to_brightness(0.5)

    def test_none_when_luminance_gain_absent(self) -> None:
        light, coordinator, _client = _make_light()
        coordinator.async_set_updated_data(_instance_data(adjustment=_adjustment(luminance_gain=None)))
        assert light.brightness is None


class TestEffectList:
    def test_prepends_solid_and_filters_hidden_effects(self) -> None:
        light, coordinator, _client = _make_light(hidden_effects={"Hidden one"})
        coordinator.async_set_updated_data(
            _instance_data(effects=[HyperHdrEffect(name="Rainbow"), HyperHdrEffect(name="Hidden one")])
        )
        assert light.effect_list == ["Solid", "Rainbow"]


class TestEffectCurrent:
    def test_visible_effect_priority_owner(self) -> None:
        light, coordinator, _client = _make_light()
        coordinator.async_set_updated_data(
            _instance_data(priorities=[_priority(component_id="EFFECT", visible=True, owner="Rainbow swirl fast")])
        )
        assert light.effect == "Rainbow swirl fast"

    def test_solid_when_color_visible(self) -> None:
        light, coordinator, _client = _make_light()
        coordinator.async_set_updated_data(
            _instance_data(priorities=[_priority(component_id="COLOR", visible=True, value={"RGB": [1, 2, 3]})])
        )
        assert light.effect == "Solid"

    def test_none_when_nothing_visible(self) -> None:
        light, coordinator, _client = _make_light()
        coordinator.async_set_updated_data(_instance_data(priorities=[]))
        assert light.effect is None

    def test_none_for_other_visible_source(self) -> None:
        light, coordinator, _client = _make_light()
        coordinator.async_set_updated_data(
            _instance_data(priorities=[_priority(component_id="VIDEOGRABBER", visible=True)])
        )
        assert light.effect is None


class TestTurnOnCommandSelection:
    async def test_off_to_on_with_no_kwargs_only_powers_on(self) -> None:
        light, coordinator, client = _make_light()
        coordinator.async_set_updated_data(
            _instance_data(components={"LEDDEVICE": HyperHdrComponent(name="LEDDEVICE", enabled=False)})
        )
        await light.async_turn_on()
        assert client.calls == [("async_set_component", ("LEDDEVICE", True), {})]

    async def test_already_on_with_no_kwargs_does_nothing(self) -> None:
        light, coordinator, client = _make_light()
        coordinator.async_set_updated_data(
            _instance_data(components={"LEDDEVICE": HyperHdrComponent(name="LEDDEVICE", enabled=True)})
        )
        await light.async_turn_on()
        assert client.calls == []

    async def test_brightness_kwarg_sets_adjustment(self) -> None:
        light, coordinator, client = _make_light()
        coordinator.async_set_updated_data(
            _instance_data(components={"LEDDEVICE": HyperHdrComponent(name="LEDDEVICE", enabled=True)})
        )
        await light.async_turn_on(brightness=128)
        assert client.calls == [("async_set_adjustment", (), {"luminance_gain": pytest.approx(128 / 255, abs=0.01)})]

    async def test_non_solid_effect_kwarg_sets_effect_at_default_priority(self) -> None:
        light, coordinator, client = _make_light(default_priority=150)
        coordinator.async_set_updated_data(
            _instance_data(components={"LEDDEVICE": HyperHdrComponent(name="LEDDEVICE", enabled=True)})
        )
        await light.async_turn_on(effect="Rainbow")
        assert client.calls == [("async_set_effect", ("Rainbow", 150, 0), {})]

    async def test_solid_effect_kwarg_sets_color_from_last_color(self) -> None:
        light, coordinator, client = _make_light(default_priority=150)
        coordinator.async_set_updated_data(
            _instance_data(
                components={"LEDDEVICE": HyperHdrComponent(name="LEDDEVICE", enabled=True)},
                priorities=[_priority(component_id="COLOR", visible=True, value={"RGB": [4, 5, 6]})],
            )
        )
        assert light.rgb_color == (4, 5, 6)  # seed _last_color
        await light.async_turn_on(effect="Solid")
        assert client.calls == [("async_set_color", ((4, 5, 6), 150, 0), {})]

    async def test_solid_effect_kwarg_falls_back_to_white_with_no_last_color(self) -> None:
        light, coordinator, client = _make_light(default_priority=150)
        coordinator.async_set_updated_data(
            _instance_data(components={"LEDDEVICE": HyperHdrComponent(name="LEDDEVICE", enabled=True)})
        )
        await light.async_turn_on(effect="Solid")
        assert client.calls == [("async_set_color", ((255, 255, 255), 150, 0), {})]

    async def test_rgb_color_kwarg_sets_color(self) -> None:
        light, coordinator, client = _make_light(default_priority=150)
        coordinator.async_set_updated_data(
            _instance_data(components={"LEDDEVICE": HyperHdrComponent(name="LEDDEVICE", enabled=True)})
        )
        await light.async_turn_on(rgb_color=(10, 20, 30))
        assert client.calls == [("async_set_color", ((10, 20, 30), 150, 0), {})]

    async def test_off_then_rgb_color_powers_on_first(self) -> None:
        light, coordinator, client = _make_light(default_priority=150)
        coordinator.async_set_updated_data(
            _instance_data(components={"LEDDEVICE": HyperHdrComponent(name="LEDDEVICE", enabled=False)})
        )
        await light.async_turn_on(rgb_color=(10, 20, 30))
        assert client.calls == [
            ("async_set_component", ("LEDDEVICE", True), {}),
            ("async_set_color", ((10, 20, 30), 150, 0), {}),
        ]


class TestTurnOff:
    async def test_disables_leddevice(self) -> None:
        light, _coordinator, client = _make_light()
        await light.async_turn_off()
        assert client.calls == [("async_set_component", ("LEDDEVICE", False), {})]


class TestErrorWrapping:
    async def test_turn_on_wraps_hyperhdr_error(self) -> None:
        client = FakeDomainClient(raise_on=HyperHdrError("boom"))
        light, coordinator, _client = _make_light(client=client)
        coordinator.async_set_updated_data(
            _instance_data(components={"LEDDEVICE": HyperHdrComponent(name="LEDDEVICE", enabled=False)})
        )
        with pytest.raises(HomeAssistantError):
            await light.async_turn_on()

    async def test_turn_off_wraps_hyperhdr_error(self) -> None:
        client = FakeDomainClient(raise_on=HyperHdrError("boom"))
        light, _coordinator, _client = _make_light(client=client)
        with pytest.raises(HomeAssistantError):
            await light.async_turn_off()

    async def test_client_none_raises_home_assistant_error(self) -> None:
        light, coordinator, _client = _make_light()
        coordinator.client = None
        with pytest.raises(HomeAssistantError):
            await light.async_turn_on()
