"""Tests for number.py: presence-based entity creation and single-field
adjustment writes."""

from __future__ import annotations

import asyncio

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
    HyperHdrServerData,
    HyperHdrSysInfo,
)
from custom_components.hyperhdr.number import NUMBERS, HyperHdrAdjustmentNumber, _entities_for_instance


def _adjustment(raw: dict[str, object], **typed: object) -> HyperHdrAdjustment:
    defaults: dict[str, object] = {
        "luminance_gain": None,
        "saturation_gain": None,
        "backlight_threshold": None,
        "gamma": None,
        "temperature_red": None,
        "temperature_green": None,
        "temperature_blue": None,
        "raw": raw,
    }
    defaults.update(typed)
    return HyperHdrAdjustment(**defaults)  # type: ignore[arg-type]


def _instance_data(adjustment: HyperHdrAdjustment) -> HyperHdrInstanceData:
    return HyperHdrInstanceData(
        instance_id=1,
        components={},
        priorities=[],
        priorities_autoselect=False,
        adjustment=adjustment,
        effects=[],
        led_count=0,
        video_mode="",
        hdr_mode=0,
        connected=True,
    )


def _make_setup(
    adjustment: HyperHdrAdjustment,
) -> tuple[FakeConfigEntry, HyperHdrInstanceCoordinator, FakeDomainClient]:
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
    # connected=True in _instance_data means wait_for_connected_data's fast
    # path (no actual waiting) applies -- these tests are about entity-set
    # building logic, not the bounded-wait mechanism itself.
    coordinator.async_set_updated_data(_instance_data(adjustment))
    client = FakeDomainClient()
    coordinator.client = client  # type: ignore[assignment]
    return entry, coordinator, client


class TestPresenceBasedCreation:
    async def test_all_fields_present_creates_all_seven(self) -> None:
        raw = {
            "luminanceGain": 1,
            "saturationGain": 1,
            "gamma": 1.5,
            "backlightThreshold": 0.0039,
            "temperatureRed": 1,
            "temperatureGreen": 1,
            "temperatureBlue": 1,
        }
        entry, coordinator, _client = _make_setup(_adjustment(raw))
        entities = await _entities_for_instance(entry, coordinator, 1)
        assert {e.entity_description.key for e in entities} == {d.key for d in NUMBERS}

    async def test_no_fields_present_creates_nothing(self) -> None:
        entry, coordinator, _client = _make_setup(_adjustment({}))
        entities = await _entities_for_instance(entry, coordinator, 1)
        assert entities == []

    async def test_only_some_fields_present_creates_only_those(self) -> None:
        raw = {"luminanceGain": 1, "gamma": 1.5}
        entry, coordinator, _client = _make_setup(_adjustment(raw))
        entities = await _entities_for_instance(entry, coordinator, 1)
        assert {e.entity_description.key for e in entities} == {"luminance_gain", "gamma"}

    async def test_unique_ids(self) -> None:
        raw = {"luminanceGain": 1}
        entry, coordinator, _client = _make_setup(_adjustment(raw))
        entities = await _entities_for_instance(entry, coordinator, 1)
        assert entities[0]._attr_unique_id == "srv-uid_1_luminance_gain"
        assert entities[0]._attr_entity_category is not None  # CONFIG

    async def test_waits_for_connected_data_instead_of_building_off_the_disconnected_placeholder(self) -> None:
        """Regression (found live, Phase 5+6): see the identical test in
        test_switch.py for the full rationale -- a freshly constructed
        coordinator's ``.data`` is the disconnected placeholder (empty
        ``adjustment.raw``) until its client's connect handshake completes;
        building the presence-gated number set against that placeholder
        would silently create zero entities, permanently, for this pass."""
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
        assert coordinator.data.connected is False
        assert coordinator.data.adjustment.raw == {}

        async def _connect_shortly_after() -> None:
            await asyncio.sleep(0)
            coordinator.async_set_updated_data(_instance_data(_adjustment({"luminanceGain": 1})))

        populate_task = asyncio.ensure_future(_connect_shortly_after())
        entities = await _entities_for_instance(entry, coordinator, 1)
        await populate_task

        assert {e.entity_description.key for e in entities} == {"luminance_gain"}


class TestNativeValue:
    async def test_reads_typed_field(self) -> None:
        raw = {"luminanceGain": 1}
        entry, coordinator, _client = _make_setup(_adjustment(raw, luminance_gain=0.42))
        entities = await _entities_for_instance(entry, coordinator, 1)
        assert entities[0].native_value == 0.42


class TestAsyncSetNativeValue:
    async def test_sets_single_field(self) -> None:
        raw = {"luminanceGain": 1}
        entry, coordinator, client = _make_setup(_adjustment(raw))
        entities = await _entities_for_instance(entry, coordinator, 1)
        await entities[0].async_set_native_value(0.7)
        assert client.calls == [("async_set_adjustment", (), {"luminance_gain": 0.7})]

    async def test_temperature_field_rounds_to_int(self) -> None:
        raw = {"temperatureRed": 1}
        entry, coordinator, client = _make_setup(_adjustment(raw))
        entities = await _entities_for_instance(entry, coordinator, 1)
        assert entities[0].entity_description.key == "temperature_red"
        await entities[0].async_set_native_value(128.0)
        assert client.calls == [("async_set_adjustment", (), {"temperature_red": 128})]
        assert isinstance(client.calls[0][2]["temperature_red"], int)

    async def test_wraps_hyperhdr_error(self) -> None:
        raw = {"luminanceGain": 1}
        entry, coordinator, _client = _make_setup(_adjustment(raw))
        coordinator.client = FakeDomainClient(raise_on=HyperHdrError("boom"))  # type: ignore[assignment]
        entities = await _entities_for_instance(entry, coordinator, 1)
        with pytest.raises(HomeAssistantError):
            await entities[0].async_set_native_value(0.1)


class TestRanges:
    def test_every_description_has_min_max_step(self) -> None:
        for description in NUMBERS:
            assert description.native_min_value is not None
            assert description.native_max_value is not None
            assert description.native_step is not None
            assert description.native_min_value < description.native_max_value


def test_number_entity_class_reexported() -> None:
    # Sanity: the module exports the entity class tests above rely on
    # implicitly via _entities_for_instance's return type.
    assert HyperHdrAdjustmentNumber is not None
