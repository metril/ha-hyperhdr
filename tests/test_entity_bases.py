"""Tests for the server/instance base entity classes."""

from __future__ import annotations

from conftest import FakeConfigEntry, FakeHass

from custom_components.hyperhdr.const import DOMAIN, MANUFACTURER
from custom_components.hyperhdr.coordinator import (
    HyperHdrInstanceCoordinator,
    HyperHdrRuntimeData,
    HyperHdrServerCoordinator,
)
from custom_components.hyperhdr.entity import HyperHdrInstanceEntity, HyperHdrServerEntity, server_uid
from custom_components.hyperhdr.models import (
    HyperHdrInstanceSummary,
    HyperHdrServerData,
    HyperHdrSysInfo,
)


def _server_coordinator(hass: FakeHass, entry: FakeConfigEntry, **data_kwargs: object) -> HyperHdrServerCoordinator:
    coordinator = HyperHdrServerCoordinator(hass, entry, object())  # type: ignore[arg-type]
    defaults = {
        "sysinfo": HyperHdrSysInfo(id="dev-id", hostname="hyperhdr-host", version="22.0.0beta2", build="b1"),
        "instances": {},
        "connected": True,
    }
    defaults.update(data_kwargs)
    coordinator.async_set_updated_data(HyperHdrServerData(**defaults))  # type: ignore[arg-type]
    return coordinator


class TestServerUidHelper:
    def test_uses_unique_id_when_set(self) -> None:
        entry = FakeConfigEntry(entry_id="entry1", unique_id="the-unique-id")
        assert server_uid(entry) == "the-unique-id"

    def test_falls_back_to_entry_id_when_unique_id_is_none(self) -> None:
        entry = FakeConfigEntry(entry_id="entry1", unique_id=None)
        assert server_uid(entry) == "entry1"


class TestHyperHdrServerEntity:
    def test_unique_id_format(self) -> None:
        hass = FakeHass()
        entry = FakeConfigEntry(entry_id="entry1", unique_id="srv-uid", data={"host": "10.0.0.5", "port": 8090})
        coordinator = _server_coordinator(hass, entry)
        entity = HyperHdrServerEntity(coordinator, entry, "uptime")
        assert entity._attr_unique_id == "srv-uid_uptime"

    def test_device_info_identifiers_name_sw_version_configuration_url(self) -> None:
        hass = FakeHass()
        entry = FakeConfigEntry(
            entry_id="entry1", unique_id="srv-uid", data={"host": "10.0.0.5", "port": 8090, "use_ssl": False}
        )
        coordinator = _server_coordinator(hass, entry)
        entity = HyperHdrServerEntity(coordinator, entry, "uptime")
        info = entity._attr_device_info
        assert info["identifiers"] == {(DOMAIN, "srv-uid")}
        assert info["manufacturer"] == MANUFACTURER
        assert info["name"] == "hyperhdr-host"
        assert info["sw_version"] == "22.0.0beta2"
        assert info["configuration_url"] == "http://10.0.0.5:8090"

    def test_configuration_url_uses_https_when_use_ssl(self) -> None:
        hass = FakeHass()
        entry = FakeConfigEntry(
            entry_id="entry1", unique_id="srv-uid", data={"host": "10.0.0.5", "port": 8090, "use_ssl": True}
        )
        coordinator = _server_coordinator(hass, entry)
        entity = HyperHdrServerEntity(coordinator, entry, "uptime")
        assert entity._attr_device_info["configuration_url"] == "https://10.0.0.5:8090"

    def test_falls_back_to_entry_title_when_no_coordinator_data(self) -> None:
        hass = FakeHass()
        entry = FakeConfigEntry(entry_id="entry1", unique_id="srv-uid", title="My HyperHDR", data={})
        coordinator = HyperHdrServerCoordinator(hass, entry, object())  # type: ignore[arg-type]
        assert coordinator.data is None
        entity = HyperHdrServerEntity(coordinator, entry, "uptime")
        assert entity._attr_device_info["name"] == "My HyperHDR"
        assert entity._attr_device_info["sw_version"] is None


class TestHyperHdrInstanceEntity:
    def _entry_with_runtime(self) -> tuple[FakeHass, FakeConfigEntry, HyperHdrServerCoordinator]:
        hass = FakeHass()
        entry = FakeConfigEntry(entry_id="entry1", unique_id="srv-uid", data={"host": "10.0.0.5", "port": 8090})
        server_coordinator = _server_coordinator(
            hass, entry, instances={1: HyperHdrInstanceSummary(instance=1, friendly_name="Living Room", running=True)}
        )
        entry.runtime_data = HyperHdrRuntimeData(  # type: ignore[attr-defined]
            server_client=object(),  # type: ignore[arg-type]
            server_coordinator=server_coordinator,
            instance_coordinators={},
            default_priority=128,
            hidden_effects=set(),
        )
        return hass, entry, server_coordinator

    def test_unique_id_format(self) -> None:
        hass, entry, _server_coordinator = self._entry_with_runtime()
        coordinator = HyperHdrInstanceCoordinator(hass, entry, 1)
        entity = HyperHdrInstanceEntity(coordinator, entry, 1, "power")
        assert entity._attr_unique_id == "srv-uid_1_power"

    def test_device_identifiers_via_device_and_name_from_roster(self) -> None:
        hass, entry, _server_coordinator = self._entry_with_runtime()
        coordinator = HyperHdrInstanceCoordinator(hass, entry, 1)
        entity = HyperHdrInstanceEntity(coordinator, entry, 1, "power")
        info = entity._attr_device_info
        assert info["identifiers"] == {(DOMAIN, "srv-uid_1")}
        assert info["via_device"] == (DOMAIN, "srv-uid")
        assert info["manufacturer"] == MANUFACTURER
        assert info["name"] == "Living Room"

    def test_device_name_falls_back_when_instance_missing_from_roster(self) -> None:
        hass, entry, _server_coordinator = self._entry_with_runtime()
        coordinator = HyperHdrInstanceCoordinator(hass, entry, 99)
        entity = HyperHdrInstanceEntity(coordinator, entry, 99, "power")
        assert entity._attr_device_info["name"] == "Instance 99"

    def test_available_reflects_data_connected_true(self) -> None:
        hass, entry, _server_coordinator = self._entry_with_runtime()
        coordinator = HyperHdrInstanceCoordinator(hass, entry, 1)
        coordinator.async_set_updated_data(
            coordinator.data.__class__(
                instance_id=1,
                components={},
                priorities=[],
                priorities_autoselect=False,
                adjustment=coordinator.data.adjustment,
                effects=[],
                led_count=0,
                video_mode="",
                hdr_mode=0,
                connected=True,
            )
        )
        entity = HyperHdrInstanceEntity(coordinator, entry, 1, "power")
        assert entity.available is True

    def test_available_is_false_when_data_connected_false(self) -> None:
        hass, entry, _server_coordinator = self._entry_with_runtime()
        coordinator = HyperHdrInstanceCoordinator(hass, entry, 1)
        # Freshly constructed coordinator seeds connected=False by default.
        entity = HyperHdrInstanceEntity(coordinator, entry, 1, "power")
        assert coordinator.data.connected is False
        assert entity.available is False

    def test_available_is_false_when_coordinator_last_update_failed(self) -> None:
        hass, entry, _server_coordinator = self._entry_with_runtime()
        coordinator = HyperHdrInstanceCoordinator(hass, entry, 1)
        entity = HyperHdrInstanceEntity(coordinator, entry, 1, "power")
        coordinator.last_update_success = False
        assert entity.available is False
