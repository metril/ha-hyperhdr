"""Tests for switch.py: dynamic component-switch build from data.components,
and the roster-driven running switch."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import FakeConfigEntry, FakeDomainClient, FakeHass
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.hyperhdr.const import SIGNAL_INSTANCE_ADDED, SIGNAL_INSTANCE_READY
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
from custom_components.hyperhdr.switch import (
    HyperHdrComponentSwitch,
    HyperHdrRunningSwitch,
    _component_entities,
    async_setup_entry,
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
        entities = _component_entities(entry, coordinator.data, coordinator, 1)
        keys = {e._attr_unique_id for e in entities}
        assert keys == {
            "srv-uid_1_component_all",
            "srv-uid_1_component_leddevice",
            "srv-uid_1_component_smoothing",
        }

    async def test_known_components_get_translation_keys_unknown_get_title_case_name(self) -> None:
        entry, _server, coordinator, _client = _make_setup()
        coordinator.async_set_updated_data(
            _instance_data(
                {
                    "ALL": HyperHdrComponent(name="ALL", enabled=True),
                    "CUSTOMTHING": HyperHdrComponent(name="CUSTOMTHING", enabled=True),
                }
            )
        )
        entities = {e._component_name: e for e in _component_entities(entry, coordinator.data, coordinator, 1)}
        # "All instances (global)" is HyperHDR's own web-UI label for ALL --
        # setting it pauses/resumes every instance on the server, so the name
        # must state that scope.
        assert entities["ALL"]._attr_translation_key == "all"
        assert entities["CUSTOMTHING"]._attr_name == "Customthing"
        assert not hasattr(entities["CUSTOMTHING"], "_attr_translation_key")

    async def test_icons_from_component_icons_with_no_icon_fallback(self) -> None:
        entry, _server, coordinator, _client = _make_setup()
        coordinator.async_set_updated_data(
            _instance_data(
                {
                    "ALL": HyperHdrComponent(name="ALL", enabled=True),
                    "LEDDEVICE": HyperHdrComponent(name="LEDDEVICE", enabled=True),
                    "CUSTOMTHING": HyperHdrComponent(name="CUSTOMTHING", enabled=True),
                }
            )
        )
        entities = {e._component_name: e for e in _component_entities(entry, coordinator.data, coordinator, 1)}
        assert entities["ALL"]._attr_icon == "mdi:power"
        assert entities["LEDDEVICE"]._attr_icon == "mdi:led-strip-variant"
        assert entities["CUSTOMTHING"]._attr_icon is None

    async def test_empty_components_creates_nothing(self) -> None:
        entry, _server, coordinator, _client = _make_setup()
        coordinator.async_set_updated_data(_instance_data({}))
        assert _component_entities(entry, coordinator.data, coordinator, 1) == []

    async def test_is_on_reflects_component_enabled(self) -> None:
        entry, _server, coordinator, _client = _make_setup()
        coordinator.async_set_updated_data(
            _instance_data({"SMOOTHING": HyperHdrComponent(name="SMOOTHING", enabled=True)})
        )
        entities = _component_entities(entry, coordinator.data, coordinator, 1)
        assert entities[0].is_on is True

    async def test_turn_on_off_call_set_component(self) -> None:
        entry, _server, coordinator, client = _make_setup()
        coordinator.async_set_updated_data(
            _instance_data({"SMOOTHING": HyperHdrComponent(name="SMOOTHING", enabled=False)})
        )
        entities = _component_entities(entry, coordinator.data, coordinator, 1)
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
        entities = _component_entities(entry, coordinator.data, coordinator, 1)
        with pytest.raises(HomeAssistantError):
            await entities[0].async_turn_on()

    async def test_listener_connected_before_any_connected_data_and_late_snapshot_adds_switches(self) -> None:
        """The READY listener is connected up front (an instance created
        while setup runs is not missed) and a connected snapshot arriving
        after setup -- e.g. past the old bounded wait -- still adds its
        switches; later updates never duplicate them."""
        hass = FakeHass()
        entry, _server, coordinator, _client = _make_setup()
        assert coordinator.data.connected is False
        added = await _setup_platform(entry, hass, async_setup_entry)
        assert [e for e in added if isinstance(e, HyperHdrComponentSwitch)] == []

        coordinator.async_set_updated_data(
            _instance_data({"SMOOTHING": HyperHdrComponent(name="SMOOTHING", enabled=True)})
        )
        coordinator.async_set_updated_data(
            _instance_data(
                {
                    "SMOOTHING": HyperHdrComponent(name="SMOOTHING", enabled=True),
                    "HDR": HyperHdrComponent(name="HDR", enabled=False),
                }
            )
        )
        coordinator.async_set_updated_data(
            _instance_data({"SMOOTHING": HyperHdrComponent(name="SMOOTHING", enabled=True)})
        )
        keys = [e._attr_unique_id for e in added if isinstance(e, HyperHdrComponentSwitch)]
        assert keys == ["srv-uid_1_component_smoothing", "srv-uid_1_component_hdr"]

    async def test_instance_ready_signal_after_setup_is_tracked_once(self) -> None:
        hass = FakeHass()
        entry, _server, coordinator, _client = _make_setup()
        del entry.runtime_data.instance_coordinators[1]
        added = await _setup_platform(entry, hass, async_setup_entry)

        coordinator.async_set_updated_data(_instance_data({"ALL": HyperHdrComponent(name="ALL", enabled=True)}))
        entry.runtime_data.instance_coordinators[1] = coordinator
        async_dispatcher_send(hass, f"{SIGNAL_INSTANCE_READY}_{entry.entry_id}", 1)
        async_dispatcher_send(hass, f"{SIGNAL_INSTANCE_READY}_{entry.entry_id}", 1)
        await hass.async_block_till_done()

        assert [e._attr_unique_id for e in added if isinstance(e, HyperHdrComponentSwitch)] == [
            "srv-uid_1_component_all"
        ]


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


def _make_entry_for_setup(hass: FakeHass, instances: dict[int, HyperHdrInstanceSummary]) -> FakeConfigEntry:
    entry = FakeConfigEntry(entry_id="entry1", unique_id="srv-uid", data={"host": "10.0.0.5", "port": 8090})
    server_coordinator = HyperHdrServerCoordinator(hass, entry, object())  # type: ignore[arg-type]
    server_coordinator.async_set_updated_data(
        HyperHdrServerData(
            sysinfo=HyperHdrSysInfo(id="dev", hostname="host", version="22", build="b"),
            instances=instances,
            connected=True,
        )
    )
    entry.runtime_data = HyperHdrRuntimeData(  # type: ignore[attr-defined]
        server_client=FakeDomainClient(),  # type: ignore[arg-type]
        server_coordinator=server_coordinator,
        # Deliberately empty -- these tests exercise only the running-switch
        # paths; a coordinator here would make async_setup_entry bounded-wait
        # on component data.
        instance_coordinators={},
        default_priority=128,
        hidden_effects=set(),
    )
    return entry


class TestRunningSwitchNeverBuiltForFirstInstance:
    """HyperHDR forbids stopping/deleting instance 0 (isInstAllowed is
    ``inst > 0``) and acks its stopInstance as a silent no-op success, so a
    Running switch for it can never work in either direction -- both add
    paths must skip it."""

    async def test_initial_setup_skips_instance_zero(self) -> None:
        hass = FakeHass()
        entry = _make_entry_for_setup(
            hass,
            instances={
                0: HyperHdrInstanceSummary(instance=0, friendly_name="First", running=True),
                1: HyperHdrInstanceSummary(instance=1, friendly_name="Second", running=True),
            },
        )
        added: list[HyperHdrRunningSwitch] = []
        await async_setup_entry(hass, entry, lambda new: added.extend(new))  # type: ignore[arg-type]
        assert [switch._instance_id for switch in added] == [1]

    async def test_dispatcher_add_skips_instance_zero(self) -> None:
        hass = FakeHass()
        entry = _make_entry_for_setup(
            hass,
            instances={0: HyperHdrInstanceSummary(instance=0, friendly_name="First", running=True)},
        )
        added: list[HyperHdrRunningSwitch] = []
        await async_setup_entry(hass, entry, lambda new: added.extend(new))  # type: ignore[arg-type]
        assert added == []

        listener = hass.dispatcher_listeners[f"{SIGNAL_INSTANCE_ADDED}_{entry.entry_id}"][0]
        await listener(0)
        assert added == []
        await listener(2)
        assert [switch._instance_id for switch in added] == [2]


async def _setup_platform(entry: FakeConfigEntry, hass: FakeHass, setup: Any) -> list[Any]:
    """Run a platform's ``async_setup_entry`` collecting every entity it adds."""
    added: list[Any] = []
    await setup(hass, entry, lambda new: added.extend(new))
    return added
