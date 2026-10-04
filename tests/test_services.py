"""Tests for services.py: the device-target resolution helper (shared by
all three services) and the set_color/set_effect/clear handlers."""

from __future__ import annotations

import pytest
from conftest import FakeConfigEntry, FakeDomainClient, FakeHass, FakeServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.hyperhdr.const import DOMAIN
from custom_components.hyperhdr.coordinator import (
    HyperHdrInstanceCoordinator,
    HyperHdrRuntimeData,
    HyperHdrServerCoordinator,
)
from custom_components.hyperhdr.exceptions import HyperHdrError
from custom_components.hyperhdr.services import (
    ATTR_DURATION,
    ATTR_EFFECT,
    ATTR_PRIORITY,
    ATTR_RGB_COLOR,
    SERVICE_CLEAR,
    SERVICE_SET_COLOR,
    SERVICE_SET_EFFECT,
    _handle_clear,
    _handle_set_color,
    _handle_set_effect,
    _resolve_device,
    async_setup_services,
    resolve_targets,
)


def _make_setup(
    *, default_priority: int = 128
) -> tuple[FakeHass, FakeConfigEntry, HyperHdrInstanceCoordinator, FakeDomainClient]:
    hass = FakeHass()
    entry = FakeConfigEntry(entry_id="entry1", unique_id="srv-uid", data={"host": "10.0.0.5", "port": 8090})
    hass.config_entries.entries.append(entry)  # _resolve_device scans this

    server_coordinator = HyperHdrServerCoordinator(hass, entry, object())  # type: ignore[arg-type]
    entry.runtime_data = HyperHdrRuntimeData(  # type: ignore[attr-defined]
        server_client=object(),  # type: ignore[arg-type]
        server_coordinator=server_coordinator,
        instance_coordinators={},
        default_priority=default_priority,
        hidden_effects=set(),
    )
    coordinator = HyperHdrInstanceCoordinator(hass, entry, 0)
    entry.runtime_data.instance_coordinators[0] = coordinator
    client = FakeDomainClient()
    coordinator.client = client  # type: ignore[assignment]

    hass.device_registry.add("device-instance", {(DOMAIN, "srv-uid_0")})
    hass.device_registry.add("device-server", {(DOMAIN, "srv-uid")})
    hass.entity_registry.add("light.inst", "srv-uid_0_light", "entry1", device_id="device-instance")
    hass.entity_registry.add("sensor.srv", "srv-uid_ver", "entry1", device_id="device-server")

    return hass, entry, coordinator, client


class TestResolveDevice:
    def test_resolves_instance_device_to_entry_client_and_instance_id(self) -> None:
        hass, entry, _coordinator, client = _make_setup()

        resolved_entry, resolved_client, instance_id = _resolve_device(hass, "device-instance")  # type: ignore[arg-type]

        assert resolved_entry is entry
        assert resolved_client is client
        assert instance_id == 0

    def test_unknown_device_id_raises_service_validation_error(self) -> None:
        hass, _entry, _coordinator, _client = _make_setup()
        with pytest.raises(ServiceValidationError):
            _resolve_device(hass, "does-not-exist")  # type: ignore[arg-type]

    def test_server_device_raises_a_friendly_not_instance_error(self) -> None:
        hass, _entry, _coordinator, _client = _make_setup()
        with pytest.raises(ServiceValidationError, match="instance device"):
            _resolve_device(hass, "device-server")  # type: ignore[arg-type]

    def test_device_from_a_different_integration_raises(self) -> None:
        hass, _entry, _coordinator, _client = _make_setup()
        hass.device_registry.add("device-other", {("other_domain", "whatever")})
        with pytest.raises(ServiceValidationError):
            _resolve_device(hass, "device-other")  # type: ignore[arg-type]

    def test_disconnected_instance_raises_not_connected_error(self) -> None:
        hass, _entry, coordinator, _client = _make_setup()
        coordinator.client = None
        with pytest.raises(ServiceValidationError, match="not connected"):
            _resolve_device(hass, "device-instance")  # type: ignore[arg-type]

    def test_entry_never_set_up_is_skipped_not_crashed_on(self) -> None:
        hass = FakeHass()
        entry = FakeConfigEntry(entry_id="entry1", unique_id="srv-uid")
        hass.config_entries.entries.append(entry)  # never got runtime_data
        hass.device_registry.add("device-instance", {(DOMAIN, "srv-uid_0")})
        with pytest.raises(ServiceValidationError):
            _resolve_device(hass, "device-instance")  # type: ignore[arg-type]


class TestSetColorService:
    async def test_calls_client_with_given_rgb_priority_and_duration_converted_to_ms(self) -> None:
        hass, _entry, _coordinator, client = _make_setup()
        call = FakeServiceCall(
            hass,
            DOMAIN,
            SERVICE_SET_COLOR,
            {"device_id": "device-instance", ATTR_RGB_COLOR: (255, 0, 255), ATTR_PRIORITY: 50, ATTR_DURATION: 2.5},
        )
        await _handle_set_color(call)  # type: ignore[arg-type]
        assert client.calls == [("async_set_color", ((255, 0, 255), 50, 2500), {})]

    async def test_omitted_priority_and_duration_use_entry_default_and_zero(self) -> None:
        hass, _entry, _coordinator, client = _make_setup(default_priority=200)
        call = FakeServiceCall(
            hass, DOMAIN, SERVICE_SET_COLOR, {"device_id": "device-instance", ATTR_RGB_COLOR: (1, 2, 3)}
        )
        await _handle_set_color(call)  # type: ignore[arg-type]
        assert client.calls == [("async_set_color", ((1, 2, 3), 200, 0), {})]

    async def test_wraps_hyperhdr_error_as_home_assistant_error(self) -> None:
        hass, _entry, coordinator, _client = _make_setup()
        coordinator.client = FakeDomainClient(raise_on=HyperHdrError("boom"))  # type: ignore[assignment]
        call = FakeServiceCall(
            hass, DOMAIN, SERVICE_SET_COLOR, {"device_id": "device-instance", ATTR_RGB_COLOR: (1, 2, 3)}
        )
        with pytest.raises(HomeAssistantError):
            await _handle_set_color(call)  # type: ignore[arg-type]

    async def test_bad_device_target_propagates_service_validation_error(self) -> None:
        hass, _entry, _coordinator, _client = _make_setup()
        call = FakeServiceCall(hass, DOMAIN, SERVICE_SET_COLOR, {"device_id": "nope", ATTR_RGB_COLOR: (1, 2, 3)})
        with pytest.raises(ServiceValidationError):
            await _handle_set_color(call)  # type: ignore[arg-type]


class TestTargets:
    async def test_entity_target_resolves_to_its_instance(self) -> None:
        hass, _entry, _coordinator, client = _make_setup()
        call = FakeServiceCall(hass, DOMAIN, SERVICE_CLEAR, {"entity_id": ["light.inst"], ATTR_PRIORITY: 5})
        await _handle_clear(call)  # type: ignore[arg-type]
        assert client.calls == [("async_clear", (5,), {})]

    async def test_device_and_entity_of_same_instance_are_deduped(self) -> None:
        hass, _entry, _coordinator, client = _make_setup()
        call = FakeServiceCall(
            hass,
            DOMAIN,
            SERVICE_CLEAR,
            {"device_id": ["device-instance"], "entity_id": ["light.inst"], ATTR_PRIORITY: 5},
        )
        await _handle_clear(call)  # type: ignore[arg-type]
        assert len(client.calls) == 1

    async def test_multiple_devices_run_on_every_instance(self) -> None:
        hass, entry, _coordinator, client = _make_setup()
        coordinator2 = HyperHdrInstanceCoordinator(hass, entry, 1)
        client2 = FakeDomainClient()
        coordinator2.client = client2  # type: ignore[assignment]
        entry.runtime_data.instance_coordinators[1] = coordinator2  # type: ignore[attr-defined]
        hass.device_registry.add("device-instance2", {(DOMAIN, "srv-uid_1")})
        call = FakeServiceCall(
            hass,
            DOMAIN,
            SERVICE_SET_COLOR,
            {"device_id": ["device-instance", "device-instance2"], ATTR_RGB_COLOR: (1, 2, 3)},
        )
        await _handle_set_color(call)  # type: ignore[arg-type]
        assert client.calls == [("async_set_color", ((1, 2, 3), 128, 0), {})]
        assert client2.calls == [("async_set_color", ((1, 2, 3), 128, 0), {})]

    async def test_no_target_raises(self) -> None:
        hass, _entry, _coordinator, client = _make_setup()
        call = FakeServiceCall(hass, DOMAIN, SERVICE_CLEAR, {ATTR_PRIORITY: 5})
        with pytest.raises(ServiceValidationError, match="No HyperHDR instance targeted"):
            await resolve_targets(hass, call)  # type: ignore[arg-type]
        assert client.calls == []

    async def test_server_device_target_raises(self) -> None:
        hass, _entry, _coordinator, _client = _make_setup()
        call = FakeServiceCall(hass, DOMAIN, SERVICE_CLEAR, {"device_id": "device-server", ATTR_PRIORITY: 5})
        with pytest.raises(ServiceValidationError, match="instance device"):
            await _handle_clear(call)  # type: ignore[arg-type]

    async def test_server_entity_target_alone_resolves_nothing(self) -> None:
        hass, _entry, _coordinator, _client = _make_setup()
        call = FakeServiceCall(hass, DOMAIN, SERVICE_CLEAR, {"entity_id": "sensor.srv", ATTR_PRIORITY: 5})
        with pytest.raises(ServiceValidationError, match="No HyperHDR instance targeted"):
            await _handle_clear(call)  # type: ignore[arg-type]

    async def test_stopped_instance_raises_not_connected(self) -> None:
        hass, _entry, coordinator, _client = _make_setup()
        coordinator.client = None
        call = FakeServiceCall(hass, DOMAIN, SERVICE_CLEAR, {"device_id": "device-instance", ATTR_PRIORITY: 5})
        with pytest.raises(ServiceValidationError, match="not connected"):
            await _handle_clear(call)  # type: ignore[arg-type]

    async def test_area_target_skips_server_foreign_and_stopped_runs_running(self) -> None:
        hass, entry, _coordinator, client = _make_setup()
        stopped = HyperHdrInstanceCoordinator(hass, entry, 1)
        entry.runtime_data.instance_coordinators[1] = stopped  # type: ignore[attr-defined]
        hass.device_registry.add("device-stopped", {(DOMAIN, "srv-uid_1")})
        hass.device_registry.add("device-foreign", {("other_domain", "x")})
        hass.area_devices["area1"] = {"device-instance", "device-server", "device-foreign", "device-stopped"}
        call = FakeServiceCall(hass, DOMAIN, SERVICE_CLEAR, {"area_id": "area1", ATTR_PRIORITY: 5})
        await _handle_clear(call)  # type: ignore[arg-type]
        assert client.calls == [("async_clear", (5,), {})]


class TestSetEffectService:
    async def test_calls_client_with_given_effect_priority_and_duration_converted_to_ms(self) -> None:
        hass, _entry, _coordinator, client = _make_setup()
        call = FakeServiceCall(
            hass,
            DOMAIN,
            SERVICE_SET_EFFECT,
            {
                "device_id": "device-instance",
                ATTR_EFFECT: "Rainbow swirl fast",
                ATTR_PRIORITY: 60,
                ATTR_DURATION: 1.0,
            },
        )
        await _handle_set_effect(call)  # type: ignore[arg-type]
        assert client.calls == [("async_set_effect", ("Rainbow swirl fast", 60, 1000), {})]

    async def test_wraps_hyperhdr_error_as_home_assistant_error(self) -> None:
        hass, _entry, coordinator, _client = _make_setup()
        coordinator.client = FakeDomainClient(raise_on=HyperHdrError("boom"))  # type: ignore[assignment]
        call = FakeServiceCall(
            hass, DOMAIN, SERVICE_SET_EFFECT, {"device_id": "device-instance", ATTR_EFFECT: "Rainbow"}
        )
        with pytest.raises(HomeAssistantError):
            await _handle_set_effect(call)  # type: ignore[arg-type]


class TestClearService:
    async def test_calls_client_with_given_priority(self) -> None:
        hass, _entry, _coordinator, client = _make_setup()
        call = FakeServiceCall(hass, DOMAIN, SERVICE_CLEAR, {"device_id": "device-instance", ATTR_PRIORITY: 128})
        await _handle_clear(call)  # type: ignore[arg-type]
        assert client.calls == [("async_clear", (128,), {})]

    async def test_negative_one_clears_all_priorities(self) -> None:
        hass, _entry, _coordinator, client = _make_setup()
        call = FakeServiceCall(hass, DOMAIN, SERVICE_CLEAR, {"device_id": "device-instance", ATTR_PRIORITY: -1})
        await _handle_clear(call)  # type: ignore[arg-type]
        assert client.calls == [("async_clear", (-1,), {})]

    async def test_wraps_hyperhdr_error_as_home_assistant_error(self) -> None:
        hass, _entry, coordinator, _client = _make_setup()
        coordinator.client = FakeDomainClient(raise_on=HyperHdrError("boom"))  # type: ignore[assignment]
        call = FakeServiceCall(hass, DOMAIN, SERVICE_CLEAR, {"device_id": "device-instance", ATTR_PRIORITY: -1})
        with pytest.raises(HomeAssistantError):
            await _handle_clear(call)  # type: ignore[arg-type]


class TestSetupServices:
    async def test_setup_registers_all_three_services_and_is_idempotent(self) -> None:
        hass = FakeHass()
        await async_setup_services(hass)  # type: ignore[arg-type]
        assert hass.services.has_service(DOMAIN, SERVICE_SET_COLOR)
        assert hass.services.has_service(DOMAIN, SERVICE_SET_EFFECT)
        assert hass.services.has_service(DOMAIN, SERVICE_CLEAR)

        await async_setup_services(hass)  # type: ignore[arg-type]  # must not raise/duplicate

    async def test_registered_service_end_to_end_via_hass_services_async_call(self) -> None:
        """Through hass.services.async_call (schema + handler together),
        not just the handler function in isolation."""
        hass, _entry, _coordinator, client = _make_setup()
        await async_setup_services(hass)  # type: ignore[arg-type]

        await hass.services.async_call(
            DOMAIN, SERVICE_SET_COLOR, {"device_id": "device-instance", ATTR_RGB_COLOR: [10, 20, 30]}
        )

        assert client.calls == [("async_set_color", ((10, 20, 30), 128, 0), {})]
