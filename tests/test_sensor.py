"""Lean tests for sensor.py: value_fn/attrs_fn evaluation for each
description."""

from __future__ import annotations

from conftest import FakeConfigEntry, FakeHass

from custom_components.hyperhdr.coordinator import (
    HyperHdrInstanceCoordinator,
    HyperHdrRuntimeData,
    HyperHdrServerCoordinator,
)
from custom_components.hyperhdr.models import (
    HyperHdrAdjustment,
    HyperHdrInstanceData,
    HyperHdrInstanceSummary,
    HyperHdrPriority,
    HyperHdrServerData,
    HyperHdrSysInfo,
)
from custom_components.hyperhdr.sensor import (
    INSTANCE_SENSORS,
    SERVER_SENSORS,
    HyperHdrInstanceSensor,
    HyperHdrServerSensor,
)


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
        "led_count": 5,
        "video_mode": "1080p",
        "hdr_mode": 0,
        "connected": True,
    }
    defaults.update(overrides)
    return HyperHdrInstanceData(**defaults)  # type: ignore[arg-type]


def _make_setup() -> tuple[FakeConfigEntry, HyperHdrServerCoordinator, HyperHdrInstanceCoordinator]:
    hass = FakeHass()
    entry = FakeConfigEntry(entry_id="entry1", unique_id="srv-uid", data={"host": "10.0.0.5", "port": 8090})
    server_coordinator = HyperHdrServerCoordinator(hass, entry, object())  # type: ignore[arg-type]
    server_coordinator.async_set_updated_data(
        HyperHdrServerData(
            sysinfo=HyperHdrSysInfo(id="dev", hostname="host", version="22.0.0beta2", build="b"),
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
    return entry, server_coordinator, coordinator


def _description(key: str):  # type: ignore[no-untyped-def]
    return next(d for d in INSTANCE_SENSORS if d.key == key)


class TestVisiblePrioritySensor:
    def test_state_and_attrs_when_visible(self) -> None:
        entry, _server, coordinator = _make_setup()
        coordinator.async_set_updated_data(
            _instance_data(
                priorities=[
                    HyperHdrPriority(
                        priority=128,
                        component_id="COLOR",
                        origin="Home Assistant",
                        owner="",
                        active=True,
                        visible=True,
                        value={"RGB": [1, 2, 3]},
                    )
                ]
            )
        )
        sensor = HyperHdrInstanceSensor(coordinator, entry, 1, _description("visible_priority"))
        assert sensor.native_value == "Home Assistant"
        assert sensor.extra_state_attributes == {
            "priority": 128,
            "component_id": "COLOR",
            "rgb": (1, 2, 3),
            "owner": "",
        }

    def test_none_when_nothing_visible(self) -> None:
        entry, _server, coordinator = _make_setup()
        coordinator.async_set_updated_data(_instance_data(priorities=[]))
        sensor = HyperHdrInstanceSensor(coordinator, entry, 1, _description("visible_priority"))
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {}


class TestLedCountAndVideoMode:
    def test_led_count(self) -> None:
        entry, _server, coordinator = _make_setup()
        coordinator.async_set_updated_data(_instance_data(led_count=42))
        sensor = HyperHdrInstanceSensor(coordinator, entry, 1, _description("led_count"))
        assert sensor.native_value == 42
        assert sensor.extra_state_attributes is None

    def test_video_mode(self) -> None:
        entry, _server, coordinator = _make_setup()
        coordinator.async_set_updated_data(_instance_data(video_mode="720p"))
        sensor = HyperHdrInstanceSensor(coordinator, entry, 1, _description("video_mode"))
        assert sensor.native_value == "720p"


class TestServerVersionSensor:
    def test_version_from_sysinfo(self) -> None:
        entry, server_coordinator, _coordinator = _make_setup()
        sensor = HyperHdrServerSensor(server_coordinator, entry, SERVER_SENSORS[0])
        assert sensor.native_value == "22.0.0beta2"
