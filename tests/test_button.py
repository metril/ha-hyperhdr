"""Lean tests for button.py: press behavior and error wrapping."""

from __future__ import annotations

import pytest
from conftest import FakeConfigEntry, FakeDomainClient, FakeHass
from homeassistant.exceptions import HomeAssistantError

from custom_components.hyperhdr.button import HyperHdrClearPriorityButton
from custom_components.hyperhdr.coordinator import (
    HyperHdrInstanceCoordinator,
    HyperHdrRuntimeData,
    HyperHdrServerCoordinator,
)
from custom_components.hyperhdr.exceptions import HyperHdrError
from custom_components.hyperhdr.models import HyperHdrInstanceSummary, HyperHdrServerData, HyperHdrSysInfo


def _make_setup(
    default_priority: int = 150,
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
        default_priority=default_priority,
        hidden_effects=set(),
    )
    coordinator = HyperHdrInstanceCoordinator(hass, entry, 1)
    entry.runtime_data.instance_coordinators[1] = coordinator
    client = FakeDomainClient()
    coordinator.client = client  # type: ignore[assignment]
    return entry, coordinator, client


class TestClearPriorityButton:
    def test_unique_id(self) -> None:
        entry, coordinator, _client = _make_setup()
        button = HyperHdrClearPriorityButton(coordinator, entry, 1)
        assert button._attr_unique_id == "srv-uid_1_clear_priority"

    async def test_press_clears_default_priority(self) -> None:
        entry, coordinator, client = _make_setup(default_priority=150)
        button = HyperHdrClearPriorityButton(coordinator, entry, 1)
        await button.async_press()
        assert client.calls == [("async_clear", (150,), {})]

    async def test_wraps_hyperhdr_error(self) -> None:
        entry, coordinator, _client = _make_setup()
        coordinator.client = FakeDomainClient(raise_on=HyperHdrError("boom"))  # type: ignore[assignment]
        button = HyperHdrClearPriorityButton(coordinator, entry, 1)
        with pytest.raises(HomeAssistantError):
            await button.async_press()

    async def test_client_none_raises_home_assistant_error(self) -> None:
        entry, coordinator, _client = _make_setup()
        coordinator.client = None
        button = HyperHdrClearPriorityButton(coordinator, entry, 1)
        with pytest.raises(HomeAssistantError):
            await button.async_press()
