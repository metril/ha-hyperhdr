"""Tests for the instance-lifecycle diff logic and its orchestration.

Two layers:

- ``_diff_instances`` (pure, in coordinator.py): given an old/new roster,
  what changed. TDD'd RED-first -- these are the semantics later phases
  build against sight-unseen, so they're pinned exactly per the Phase 3
  brief.
- The diff *handler* (``custom_components.hyperhdr._async_handle_instance_diff``
  and friends, in __init__.py): given a diff, what HA-side side effects fire
  (coordinator/client lifecycle, dispatcher signals, registry purge).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from conftest import FakeConfigEntry, FakeHass, FakeInstanceClient

from custom_components.hyperhdr.const import DOMAIN, SIGNAL_INSTANCE_ADDED, SIGNAL_INSTANCE_READY
from custom_components.hyperhdr.coordinator import (
    HyperHdrInstanceCoordinator,
    HyperHdrRuntimeData,
    HyperHdrServerCoordinator,
    _diff_instances,
)
from custom_components.hyperhdr.models import HyperHdrInstanceSummary, HyperHdrServerData, HyperHdrSysInfo

if TYPE_CHECKING:
    import pytest

# --- _diff_instances (pure) -------------------------------------------------


def _summary(instance: int, running: bool, name: str = "inst") -> HyperHdrInstanceSummary:
    return HyperHdrInstanceSummary(instance=instance, friendly_name=name, running=running)


class TestDiffInstancesFreshAppear:
    def test_new_instance_not_running_is_only_created(self) -> None:
        diff = _diff_instances({}, {1: _summary(1, running=False)})
        assert diff.created == [1]
        assert diff.started == []
        assert diff.stopped == []
        assert diff.removed == []

    def test_new_instance_already_running_is_created_and_started(self) -> None:
        diff = _diff_instances({}, {1: _summary(1, running=True)})
        assert diff.created == [1]
        assert diff.started == [1]
        assert diff.stopped == []
        assert diff.removed == []


class TestDiffInstancesStart:
    def test_running_flips_false_to_true_is_started(self) -> None:
        old = {1: _summary(1, running=False)}
        new = {1: _summary(1, running=True)}
        diff = _diff_instances(old, new)
        assert diff.created == []
        assert diff.started == [1]
        assert diff.stopped == []
        assert diff.removed == []


class TestDiffInstancesStop:
    def test_running_flips_true_to_false_is_stopped(self) -> None:
        old = {1: _summary(1, running=True)}
        new = {1: _summary(1, running=False)}
        diff = _diff_instances(old, new)
        assert diff.created == []
        assert diff.started == []
        assert diff.stopped == [1]
        assert diff.removed == []

    def test_no_change_produces_empty_diff(self) -> None:
        old = {1: _summary(1, running=True)}
        new = {1: _summary(1, running=True)}
        diff = _diff_instances(old, new)
        assert diff == ([], [], [], [])


class TestDiffInstancesRestartCycle:
    def test_start_then_stop_then_start_each_step_independently(self) -> None:
        stopped_state = {1: _summary(1, running=False)}
        running_state = {1: _summary(1, running=True)}

        first_start = _diff_instances(stopped_state, running_state)
        assert first_start.started == [1]
        assert first_start.stopped == []

        then_stop = _diff_instances(running_state, stopped_state)
        assert then_stop.stopped == [1]
        assert then_stop.started == []

        then_restart = _diff_instances(stopped_state, running_state)
        assert then_restart.started == [1]
        assert then_restart.stopped == []


class TestDiffInstancesDelete:
    def test_running_instance_removed_is_only_removed_not_stopped(self) -> None:
        old = {1: _summary(1, running=True)}
        diff = _diff_instances(old, {})
        assert diff.removed == [1]
        assert diff.stopped == []
        assert diff.created == []
        assert diff.started == []

    def test_non_running_instance_removed_is_only_removed(self) -> None:
        old = {1: _summary(1, running=False)}
        diff = _diff_instances(old, {})
        assert diff.removed == [1]
        assert diff.created == []


class TestDiffInstancesSimultaneousMultiInstance:
    def test_mixed_created_started_stopped_removed_in_one_call(self) -> None:
        old = {
            0: _summary(0, running=True),  # unchanged
            1: _summary(1, running=False),  # will start
            2: _summary(2, running=True),  # will be removed
        }
        new = {
            0: _summary(0, running=True),
            1: _summary(1, running=True),
            3: _summary(3, running=False),  # newly created, not running
        }
        diff = _diff_instances(old, new)
        assert diff.created == [3]
        assert diff.started == [1]
        assert diff.stopped == []
        assert diff.removed == [2]


class TestDiffInstancesEmptyPopulated:
    def test_empty_to_populated(self) -> None:
        new = {0: _summary(0, running=True), 1: _summary(1, running=False)}
        diff = _diff_instances({}, new)
        assert diff.created == [0, 1]
        assert diff.started == [0]
        assert diff.stopped == []
        assert diff.removed == []

    def test_populated_to_empty(self) -> None:
        old = {0: _summary(0, running=True), 1: _summary(1, running=False)}
        diff = _diff_instances(old, {})
        assert diff.created == []
        assert diff.started == []
        assert diff.stopped == []
        assert diff.removed == [0, 1]

    def test_both_empty(self) -> None:
        diff = _diff_instances({}, {})
        assert diff == ([], [], [], [])


class TestDiffInstancesOrdering:
    def test_output_lists_are_sorted_regardless_of_dict_insertion_order(self) -> None:
        old = {5: _summary(5, running=False), 2: _summary(2, running=False)}
        new = {5: _summary(5, running=True), 2: _summary(2, running=True), 9: _summary(9, running=True)}
        diff = _diff_instances(old, new)
        # 9 is newly created AND already running -- appears in both lists.
        assert diff.started == [2, 5, 9]
        assert diff.created == [9]


# --- diff handler orchestration ---------------------------------------------
#
# Exercised via the real __init__.py module (monkeypatching the instance
# client factory + HyperHdrInstanceCoordinator) so these tests double as
# behavioral coverage for _async_handle_instance_diff/_async_start_instance/
# _async_stop_instance/_async_remove_instance without needing a live socket.

import custom_components.hyperhdr as hyperhdr  # noqa: E402


def _make_runtime(
    hass: FakeHass, entry: FakeConfigEntry, roster: dict[int, HyperHdrInstanceSummary]
) -> HyperHdrRuntimeData:
    server_client = object()
    server_coordinator = HyperHdrServerCoordinator(hass, entry, server_client)  # type: ignore[arg-type]
    server_coordinator.async_set_updated_data(
        HyperHdrServerData(
            sysinfo=HyperHdrSysInfo(id="x", hostname="host", version="22", build="b"),
            instances=roster,
            connected=True,
        )
    )
    return HyperHdrRuntimeData(
        server_client=server_client,  # type: ignore[arg-type]
        server_coordinator=server_coordinator,
        instance_coordinators={},
        default_priority=128,
        hidden_effects=set(),
    )


def _entry_with_runtime(roster: dict[int, HyperHdrInstanceSummary]) -> tuple[FakeHass, FakeConfigEntry]:
    hass = FakeHass()
    entry = FakeConfigEntry(entry_id="entry1", unique_id="server-uid")
    entry.runtime_data = _make_runtime(hass, entry, roster)  # type: ignore[attr-defined]
    return hass, entry


class TestDiffHandlerCreatedNotRunning:
    async def test_created_not_running_fires_added_signal_no_factory_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hass, entry = _entry_with_runtime({})
        factory_calls: list[int] = []

        async def _fake_factory(hass: Any, entry: Any, instance_id: int) -> FakeInstanceClient:
            factory_calls.append(instance_id)
            return FakeInstanceClient(instance_id)

        monkeypatch.setattr(hyperhdr, "_async_create_instance_client", _fake_factory)

        new_roster = {1: _summary(1, running=False)}
        await hyperhdr._async_handle_instance_diff(hass, entry, new_roster)

        assert hass.dispatcher_calls[f"{SIGNAL_INSTANCE_ADDED}_{entry.entry_id}"] == [(1,)]
        assert f"{SIGNAL_INSTANCE_READY}_{entry.entry_id}" not in hass.dispatcher_calls
        assert factory_calls == []
        assert entry.runtime_data.instance_coordinators == {}  # type: ignore[attr-defined]


class TestDiffHandlerStartedNew:
    async def test_started_new_instance_creates_coordinator_and_fires_ready_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hass, entry = _entry_with_runtime({})
        factory_calls: list[int] = []
        created_clients: list[FakeInstanceClient] = []

        async def _fake_factory(hass: Any, entry: Any, instance_id: int) -> FakeInstanceClient:
            factory_calls.append(instance_id)
            client = FakeInstanceClient(instance_id)
            created_clients.append(client)
            return client

        monkeypatch.setattr(hyperhdr, "_async_create_instance_client", _fake_factory)

        new_roster = {1: _summary(1, running=True)}
        await hyperhdr._async_handle_instance_diff(hass, entry, new_roster)

        assert factory_calls == [1]
        runtime = entry.runtime_data  # type: ignore[attr-defined]
        assert set(runtime.instance_coordinators) == {1}
        coordinator = runtime.instance_coordinators[1]
        assert coordinator.client is created_clients[0]
        assert coordinator.data is not None
        assert coordinator.data.connected is False  # seeded, not yet actually connected
        assert created_clients[0].start_calls == 1
        assert hass.dispatcher_calls[f"{SIGNAL_INSTANCE_READY}_{entry.entry_id}"] == [(1,)]
        # created-already-running: ALSO fires ADDED.
        assert hass.dispatcher_calls[f"{SIGNAL_INSTANCE_ADDED}_{entry.entry_id}"] == [(1,)]


class TestDiffHandlerStartedExisting:
    async def test_started_existing_coordinator_attaches_fresh_client_no_duplicate_ready(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hass, entry = _entry_with_runtime({1: _summary(1, running=False)})
        existing_coordinator = HyperHdrInstanceCoordinator(hass, entry, 1)
        entry.runtime_data.instance_coordinators[1] = existing_coordinator  # type: ignore[attr-defined]

        factory_calls: list[int] = []
        created_clients: list[FakeInstanceClient] = []

        async def _fake_factory(hass: Any, entry: Any, instance_id: int) -> FakeInstanceClient:
            factory_calls.append(instance_id)
            client = FakeInstanceClient(instance_id)
            created_clients.append(client)
            return client

        monkeypatch.setattr(hyperhdr, "_async_create_instance_client", _fake_factory)

        new_roster = {1: _summary(1, running=True)}
        await hyperhdr._async_handle_instance_diff(hass, entry, new_roster)

        assert factory_calls == [1]
        runtime = entry.runtime_data  # type: ignore[attr-defined]
        assert runtime.instance_coordinators[1] is existing_coordinator  # same coordinator, not recreated
        assert existing_coordinator.client is created_clients[0]
        assert created_clients[0].start_calls == 1
        assert f"{SIGNAL_INSTANCE_READY}_{entry.entry_id}" not in hass.dispatcher_calls


class TestDiffHandlerStopped:
    async def test_stopped_detaches_and_stops_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hass, entry = _entry_with_runtime({1: _summary(1, running=True)})
        coordinator = HyperHdrInstanceCoordinator(hass, entry, 1)
        client = FakeInstanceClient(1)
        coordinator.attach_client(client)  # type: ignore[arg-type]
        entry.runtime_data.instance_coordinators[1] = coordinator  # type: ignore[attr-defined]

        async def _unused_factory(hass: Any, entry: Any, instance_id: int) -> FakeInstanceClient:
            raise AssertionError("factory should not be called for a stop transition")

        monkeypatch.setattr(hyperhdr, "_async_create_instance_client", _unused_factory)

        new_roster = {1: _summary(1, running=False)}
        await hyperhdr._async_handle_instance_diff(hass, entry, new_roster)

        assert client.stop_calls == 1
        assert coordinator.client is None
        assert coordinator.data is not None
        assert coordinator.data.connected is False
        # coordinator persists -- not dropped from runtime_data on stop.
        assert 1 in entry.runtime_data.instance_coordinators  # type: ignore[attr-defined]


class TestDiffHandlerRemoved:
    async def test_removed_stops_client_drops_coordinator_purges_registry_removes_device(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hass, entry = _entry_with_runtime({1: _summary(1, running=True)})
        coordinator = HyperHdrInstanceCoordinator(hass, entry, 1)
        client = FakeInstanceClient(1)
        coordinator.attach_client(client)  # type: ignore[arg-type]
        entry.runtime_data.instance_coordinators[1] = coordinator  # type: ignore[attr-defined]

        server_uid = "server-uid"
        hass.entity_registry.add(f"switch.{server_uid}_1_running", f"{server_uid}_1_running", entry.entry_id)
        hass.entity_registry.add(f"light.{server_uid}_1_light", f"{server_uid}_1_light", entry.entry_id)
        # Different instance -- must survive the purge.
        hass.entity_registry.add(f"light.{server_uid}_2_light", f"{server_uid}_2_light", entry.entry_id)
        # Server-scoped entity -- must survive too (no trailing "_1_" prefix match).
        hass.entity_registry.add(f"sensor.{server_uid}_uptime", f"{server_uid}_uptime", entry.entry_id)
        hass.device_registry.add(f"{server_uid}_1", {(DOMAIN, f"{server_uid}_1")})

        async def _unused_factory(hass: Any, entry: Any, instance_id: int) -> FakeInstanceClient:
            raise AssertionError("factory should not be called for a removal")

        monkeypatch.setattr(hyperhdr, "_async_create_instance_client", _unused_factory)

        await hyperhdr._async_handle_instance_diff(hass, entry, {})

        assert client.stop_calls == 1
        assert 1 not in entry.runtime_data.instance_coordinators  # type: ignore[attr-defined]
        assert sorted(hass.entity_registry.removed) == [
            f"light.{server_uid}_1_light",
            f"switch.{server_uid}_1_running",
        ]
        assert list(hass.entity_registry.entities) == [
            f"light.{server_uid}_2_light",
            f"sensor.{server_uid}_uptime",
        ]
        assert hass.device_registry.removed == [f"{server_uid}_1"]


class TestDiffHandlerConcurrencyGuard:
    async def test_overlapping_invocations_serialize_and_do_not_double_create(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hass, entry = _entry_with_runtime({})
        factory_calls: list[int] = []
        release = asyncio.Event()

        async def _fake_factory(hass: Any, entry: Any, instance_id: int) -> FakeInstanceClient:
            factory_calls.append(instance_id)
            await release.wait()
            return FakeInstanceClient(instance_id)

        monkeypatch.setattr(hyperhdr, "_async_create_instance_client", _fake_factory)

        new_roster = {1: _summary(1, running=True)}
        task_a = asyncio.ensure_future(hyperhdr._async_handle_instance_diff(hass, entry, new_roster))
        await asyncio.sleep(0)
        task_b = asyncio.ensure_future(hyperhdr._async_handle_instance_diff(hass, entry, new_roster))
        await asyncio.sleep(0)

        # Task A is blocked inside the factory holding the lock; task B must
        # not have entered its own factory call yet (still queued on the lock).
        assert factory_calls == [1]

        release.set()
        await task_a
        await task_b

        # Only one factory call total: task B, run after A released the
        # lock, sees the now-existing coordinator and does not double-create.
        assert factory_calls == [1]
        assert len(entry.runtime_data.instance_coordinators) == 1  # type: ignore[attr-defined]
        assert hass.dispatcher_calls[f"{SIGNAL_INSTANCE_READY}_{entry.entry_id}"] == [(1,)]


class TestServerReconnectReconciliation:
    async def test_on_connected_after_outage_reconciles_roster_changes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A reconnect after an outage may discover an instance was deleted
        (or created/started/stopped) entirely while disconnected -- the
        diff handler run from on_connected must reconcile all of it, not
        just append."""
        hass, entry = _entry_with_runtime({1: _summary(1, running=True), 2: _summary(2, running=True)})
        coordinator1 = HyperHdrInstanceCoordinator(hass, entry, 1)
        client1 = FakeInstanceClient(1)
        coordinator1.attach_client(client1)  # type: ignore[arg-type]
        entry.runtime_data.instance_coordinators[1] = coordinator1  # type: ignore[attr-defined]
        coordinator2 = HyperHdrInstanceCoordinator(hass, entry, 2)
        client2 = FakeInstanceClient(2)
        coordinator2.attach_client(client2)  # type: ignore[arg-type]
        entry.runtime_data.instance_coordinators[2] = coordinator2  # type: ignore[attr-defined]

        async def _unused_factory(hass: Any, entry: Any, instance_id: int) -> FakeInstanceClient:
            raise AssertionError("no instance should be (re)created by this reconciliation")

        monkeypatch.setattr(hyperhdr, "_async_create_instance_client", _unused_factory)

        # While disconnected: instance 2 was deleted from the server roster.
        # Simulate the reconnect's fresh roster (from the new serverinfo).
        reconciled_roster = {1: _summary(1, running=True)}
        await hyperhdr._async_handle_instance_diff(hass, entry, reconciled_roster)

        assert client2.stop_calls == 1
        assert 2 not in entry.runtime_data.instance_coordinators  # type: ignore[attr-defined]
        assert 1 in entry.runtime_data.instance_coordinators  # type: ignore[attr-defined]
        assert entry.runtime_data.server_coordinator.data.instances == reconciled_roster  # type: ignore[attr-defined]
