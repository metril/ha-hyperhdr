"""Tests for switch.py: dynamic component-switch build from data.components,
and the roster-driven running switch."""

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
    HyperHdrComponent,
    HyperHdrInstanceData,
    HyperHdrInstanceSummary,
    HyperHdrServerData,
    HyperHdrSysInfo,
)
from custom_components.hyperhdr.switch import HyperHdrRunningSwitch, _component_entities_for_instance


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


def _instance_data(components: dict[str, HyperHdrComponent]) -> HyperHdrInstanceData:
    return HyperHdrInstanceData(
        instance_id=1,
        components=components,
        priorities=[],
        priorities_autoselect=False,
        adjustment=_adjustment(),
        effects=[],
        led_count=0,
        video_mode="",
        hdr_mode=0,
        connected=True,
    )


def _make_setup(
    instances: dict[int, HyperHdrInstanceSummary] | None = None,
) -> tuple[FakeConfigEntry, HyperHdrServerCoordinator, HyperHdrInstanceCoordinator, FakeDomainClient]:
    hass = FakeHass()
    entry = FakeConfigEntry(entry_id="entry1", unique_id="srv-uid", data={"host": "10.0.0.5", "port": 8090})
    server_coordinator = HyperHdrServerCoordinator(hass, entry, object())  # type: ignore[arg-type]
    server_coordinator.async_set_updated_data(
        HyperHdrServerData(
            sysinfo=HyperHdrSysInfo(id="dev", hostname="host", version="22", build="b"),
            instances=instances
            if instances is not None
            else {1: HyperHdrInstanceSummary(instance=1, friendly_name="Living Room", running=True)},
            connected=True,
        )
    )
    server_client = FakeDomainClient()
    entry.runtime_data = HyperHdrRuntimeData(  # type: ignore[attr-defined]
        server_client=server_client,  # type: ignore[arg-type]
        server_coordinator=server_coordinator,
        instance_coordinators={},
        default_priority=128,
        hidden_effects=set(),
    )
    coordinator = HyperHdrInstanceCoordinator(hass, entry, 1)
    entry.runtime_data.instance_coordinators[1] = coordinator
    instance_client = FakeDomainClient()
    coordinator.client = instance_client  # type: ignore[assignment]
    return entry, server_coordinator, coordinator, instance_client


class TestComponentSwitchDynamicBuild:
    async def test_one_switch_per_component_including_all_and_leddevice(self) -> None:
        entry, _server, coordinator, _client = _make_setup()
        coordinator.async_set_updated_data(
            _instance_data(
                {
                    "ALL": HyperHdrComponent(name="ALL", enabled=True),
                    "LEDDEVICE": HyperHdrComponent(name="LEDDEVICE", enabled=False),
                    "SMOOTHING": HyperHdrComponent(name="SMOOTHING", enabled=True),
                }
            )
        )
        entities = await _component_entities_for_instance(entry, coordinator, 1)
        keys = {e._attr_unique_id for e in entities}
        assert keys == {
            "srv-uid_1_component_all",
            "srv-uid_1_component_leddevice",
            "srv-uid_1_component_smoothing",
        }

    async def test_names_from_component_labels_with_title_case_fallback(self) -> None:
        entry, _server, coordinator, _client = _make_setup()
        coordinator.async_set_updated_data(
            _instance_data(
                {
                    "ALL": HyperHdrComponent(name="ALL", enabled=True),
                    "CUSTOMTHING": HyperHdrComponent(name="CUSTOMTHING", enabled=True),
                }
            )
        )
        entities = {e._component_name: e for e in await _component_entities_for_instance(entry, coordinator, 1)}
        assert entities["ALL"]._attr_name == "LED output"
        assert entities["CUSTOMTHING"]._attr_name == "Customthing"

    async def test_empty_components_creates_nothing(self) -> None:
        entry, _server, coordinator, _client = _make_setup()
        coordinator.async_set_updated_data(_instance_data({}))
        assert await _component_entities_for_instance(entry, coordinator, 1) == []

    async def test_is_on_reflects_component_enabled(self) -> None:
        entry, _server, coordinator, _client = _make_setup()
        coordinator.async_set_updated_data(
            _instance_data({"SMOOTHING": HyperHdrComponent(name="SMOOTHING", enabled=True)})
        )
        entities = await _component_entities_for_instance(entry, coordinator, 1)
        assert entities[0].is_on is True

    async def test_turn_on_off_call_set_component(self) -> None:
        entry, _server, coordinator, client = _make_setup()
        coordinator.async_set_updated_data(
            _instance_data({"SMOOTHING": HyperHdrComponent(name="SMOOTHING", enabled=False)})
        )
        entities = await _component_entities_for_instance(entry, coordinator, 1)
        await entities[0].async_turn_on()
        await entities[0].async_turn_off()
        assert client.calls == [
            ("async_set_component", ("SMOOTHING", True), {}),
            ("async_set_component", ("SMOOTHING", False), {}),
        ]

    async def test_wraps_hyperhdr_error(self) -> None:
        entry, _server, coordinator, _client = _make_setup()
        coordinator.client = FakeDomainClient(raise_on=HyperHdrError("boom"))  # type: ignore[assignment]
        coordinator.async_set_updated_data(
            _instance_data({"SMOOTHING": HyperHdrComponent(name="SMOOTHING", enabled=False)})
        )
        entities = await _component_entities_for_instance(entry, coordinator, 1)
        with pytest.raises(HomeAssistantError):
            await entities[0].async_turn_on()

    async def test_waits_for_connected_data_instead_of_building_off_the_disconnected_placeholder(self) -> None:
        """Regression (found live, Phase 5+6): a freshly constructed
        coordinator's ``.data`` is the disconnected placeholder (empty
        ``components``) until its client's connect handshake completes.
        Building the switch set against that placeholder -- which is
        exactly what happens if the builder reads ``coordinator.data``
        synchronously rather than awaiting ``wait_for_connected_data`` --
        would silently create zero switches, permanently, for this add
        pass. Confirmed live via HA's "restored": true ghost entities."""
        entry, _server, coordinator, _client = _make_setup()
        assert coordinator.data.connected is False
        assert coordinator.data.components == {}

        async def _connect_shortly_after() -> None:
            await asyncio.sleep(0)
            coordinator.async_set_updated_data(
                _instance_data({"SMOOTHING": HyperHdrComponent(name="SMOOTHING", enabled=True)})
            )

        populate_task = asyncio.ensure_future(_connect_shortly_after())
        entities = await _component_entities_for_instance(entry, coordinator, 1)
        await populate_task

        assert len(entities) == 1
        assert entities[0]._component_name == "SMOOTHING"


class TestRunningSwitch:
    def test_unique_id_format(self) -> None:
        entry, server_coordinator, _coordinator, _client = _make_setup()
        switch = HyperHdrRunningSwitch(server_coordinator, entry, 1)
        assert switch._attr_unique_id == "srv-uid_1_running"
        # No entity_category set -- unlike the component switches, this is a
        # primary control, not config/diagnostic.
        assert getattr(switch, "_attr_entity_category", None) is None

    def test_is_on_from_roster(self) -> None:
        entry, server_coordinator, _coordinator, _client = _make_setup(
            instances={1: HyperHdrInstanceSummary(instance=1, friendly_name="Living Room", running=True)}
        )
        switch = HyperHdrRunningSwitch(server_coordinator, entry, 1)
        assert switch.is_on is True

    def test_is_on_false_when_stopped(self) -> None:
        entry, server_coordinator, _coordinator, _client = _make_setup(
            instances={1: HyperHdrInstanceSummary(instance=1, friendly_name="Living Room", running=False)}
        )
        switch = HyperHdrRunningSwitch(server_coordinator, entry, 1)
        assert switch.is_on is False

    def test_is_on_false_when_instance_not_in_roster(self) -> None:
        entry, server_coordinator, _coordinator, _client = _make_setup(instances={})
        switch = HyperHdrRunningSwitch(server_coordinator, entry, 1)
        assert switch.is_on is False

    def test_available_reflects_server_connected(self) -> None:
        entry, server_coordinator, _coordinator, _client = _make_setup()
        switch = HyperHdrRunningSwitch(server_coordinator, entry, 1)
        assert switch.available is True
        server_coordinator.async_set_updated_data(
            HyperHdrServerData(
                sysinfo=HyperHdrSysInfo(id="dev", hostname="host", version="22", build="b"),
                instances={1: HyperHdrInstanceSummary(instance=1, friendly_name="Living Room", running=True)},
                connected=False,
            )
        )
        assert switch.available is False

    async def test_turn_on_calls_server_client_start_instance(self) -> None:
        entry, server_coordinator, _coordinator, _client = _make_setup()
        switch = HyperHdrRunningSwitch(server_coordinator, entry, 1)
        await switch.async_turn_on()
        assert entry.runtime_data.server_client.calls == [("start_instance", (1,), {})]  # type: ignore[attr-defined]

    async def test_turn_off_calls_server_client_stop_instance(self) -> None:
        entry, server_coordinator, _coordinator, _client = _make_setup()
        switch = HyperHdrRunningSwitch(server_coordinator, entry, 1)
        await switch.async_turn_off()
        assert entry.runtime_data.server_client.calls == [("stop_instance", (1,), {})]  # type: ignore[attr-defined]

    async def test_wraps_hyperhdr_error(self) -> None:
        entry, server_coordinator, _coordinator, _client = _make_setup()
        entry.runtime_data.server_client = FakeDomainClient(raise_on=HyperHdrError("boom"))  # type: ignore[attr-defined]
        switch = HyperHdrRunningSwitch(server_coordinator, entry, 1)
        with pytest.raises(HomeAssistantError):
            await switch.async_turn_on()

    def test_device_info_points_at_instance_device(self) -> None:
        entry, server_coordinator, _coordinator, _client = _make_setup()
        switch = HyperHdrRunningSwitch(server_coordinator, entry, 1)
        assert switch._attr_device_info["identifiers"] == {("hyperhdr", "srv-uid_1")}
