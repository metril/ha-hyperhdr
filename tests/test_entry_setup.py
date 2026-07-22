"""Tests for async_setup_entry/async_unload_entry: session selection, the
first-connect wait (success/timeout/auth-failure), runtime_data seeding,
starting already-running instances, platform forwarding, and unload
idempotency.

Uses ``FakeServerClient`` (monkeypatched in place of ``HyperHdrServerClient``)
rather than a scripted websocket -- these tests are about __init__.py's own
wiring/orchestration, not the wire protocol (that's Phase 2's job). Timeouts
are kept in the single-digit-millisecond range so the timeout-path test
doesn't rely on a fake clock.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import FakeConfigEntry, FakeHass, FakeInstanceClient, FakeServerClient

import custom_components.hyperhdr as hyperhdr
from custom_components.hyperhdr.exceptions import HyperHdrError  # noqa: F401 (sanity: real module still resolves)
from custom_components.hyperhdr.models import HyperHdrSysInfo

pytestmark = pytest.mark.usefixtures("_fast_connect_timeout")


@pytest.fixture
def _fast_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the timeout-path test's real wait_for() bounded to a few ms."""
    monkeypatch.setattr(hyperhdr, "_CONNECT_WAIT_MARGIN", 0.02)


def _entry(**data: Any) -> FakeConfigEntry:
    base = {"host": "10.0.0.5", "port": 8090}
    base.update(data)
    return FakeConfigEntry(entry_id="entry1", unique_id="srv-uid", data=base, options={"request_timeout": 0.01})


def _patch_server_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    connect_behavior: str = "success",
    connect_info: dict[str, Any] | None = None,
) -> list[FakeServerClient]:
    created: list[FakeServerClient] = []

    def _factory(session: Any, host: str, port: int, **kwargs: Any) -> FakeServerClient:
        client = FakeServerClient(session, host, port, **kwargs)
        client.sysinfo_result = HyperHdrSysInfo(id="dev", hostname="hyperhdr-host", version="22", build="b")
        client.connect_behavior = connect_behavior
        if connect_info is not None:
            client.connect_info = connect_info
        created.append(client)
        return client

    monkeypatch.setattr(hyperhdr, "HyperHdrServerClient", _factory)
    return created


class TestAsyncSetupEntryHappyPath:
    async def test_seeds_runtime_data_and_forwards_platforms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hass = FakeHass()
        entry = _entry()
        clients = _patch_server_client(monkeypatch)

        result = await hyperhdr.async_setup_entry(hass, entry)  # type: ignore[arg-type]

        assert result is True
        assert clients[0].start_calls == 1
        runtime = entry.runtime_data  # type: ignore[attr-defined]
        assert runtime.server_client is clients[0]
        assert runtime.server_coordinator.data.connected is True
        assert runtime.server_coordinator.data.sysinfo.hostname == "hyperhdr-host"
        assert runtime.instance_coordinators == {}
        assert hass.config_entries.forward_calls == [(entry, [])]
        assert entry.update_listeners  # add_update_listener was registered

    async def test_instance_update_push_before_runtime_data_assigned_does_not_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guards a narrow race: the instance-update subscription (sent as
        part of the connect handshake) can only go live once on_connected
        has already fired, but there's a brief window after that -- before
        runtime_data is assigned -- where a push could still land."""
        hass = FakeHass()
        entry = _entry()
        clients = _patch_server_client(monkeypatch)

        await hyperhdr.async_setup_entry(hass, entry)  # type: ignore[arg-type]
        push_cb = clients[0].push_callbacks[hyperhdr.INSTANCE_UPDATE_TOPIC]
        del entry.runtime_data  # type: ignore[attr-defined]

        push_cb({"data": [{"instance": 0, "friendly_name": "First", "running": True}]})
        await hass.async_block_till_done()  # must not raise

    async def test_default_priority_and_hidden_effects_come_from_options(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hass = FakeHass()
        entry = _entry()
        entry.options["default_priority"] = 200
        entry.options["hidden_effects"] = ["Rainbow swirl fast"]
        _patch_server_client(monkeypatch)

        await hyperhdr.async_setup_entry(hass, entry)  # type: ignore[arg-type]

        runtime = entry.runtime_data  # type: ignore[attr-defined]
        assert runtime.default_priority == 200
        assert runtime.hidden_effects == {"Rainbow swirl fast"}


class TestAsyncSetupEntryConnectFailure:
    async def test_timeout_raises_config_entry_not_ready_and_stops_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hass = FakeHass()
        entry = _entry()
        clients = _patch_server_client(monkeypatch, connect_behavior="timeout")

        from homeassistant.exceptions import ConfigEntryNotReady

        with pytest.raises(ConfigEntryNotReady):
            await hyperhdr.async_setup_entry(hass, entry)  # type: ignore[arg-type]

        assert clients[0].stop_calls == 1
        assert not hasattr(entry, "runtime_data")

    async def test_auth_failure_raises_config_entry_auth_failed_and_stops_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hass = FakeHass()
        entry = _entry()
        clients = _patch_server_client(monkeypatch, connect_behavior="auth_failed")

        from homeassistant.exceptions import ConfigEntryAuthFailed

        with pytest.raises(ConfigEntryAuthFailed):
            await hyperhdr.async_setup_entry(hass, entry)  # type: ignore[arg-type]

        assert clients[0].stop_calls == 1
        assert not hasattr(entry, "runtime_data")
        assert entry.reauth_started is False  # AuthFailed is raised, not proactively started


class TestAsyncSetupEntrySessionSelection:
    async def test_verify_ssl_false_with_use_ssl_selects_insecure_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hass = FakeHass()
        entry = _entry(use_ssl=True, verify_ssl=False)
        clients = _patch_server_client(monkeypatch)

        await hyperhdr.async_setup_entry(hass, entry)  # type: ignore[arg-type]

        assert clients[0].session is hass.insecure_client_session

    async def test_use_ssl_true_verify_ssl_true_selects_normal_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hass = FakeHass()
        entry = _entry(use_ssl=True, verify_ssl=True)
        clients = _patch_server_client(monkeypatch)

        await hyperhdr.async_setup_entry(hass, entry)  # type: ignore[arg-type]

        assert clients[0].session is hass.client_session

    async def test_no_ssl_selects_normal_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hass = FakeHass()
        entry = _entry(use_ssl=False)
        clients = _patch_server_client(monkeypatch)

        await hyperhdr.async_setup_entry(hass, entry)  # type: ignore[arg-type]

        assert clients[0].session is hass.client_session


class TestAsyncSetupEntryStartsRunningInstances:
    async def test_running_instances_get_coordinators_non_running_do_not(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hass = FakeHass()
        entry = _entry()
        _patch_server_client(
            monkeypatch,
            connect_info={
                "instance": [
                    {"instance": 0, "friendly_name": "First", "running": True},
                    {"instance": 1, "friendly_name": "Second", "running": False},
                ]
            },
        )
        clients_by_id: dict[int, FakeInstanceClient] = {}

        async def _fake_instance_factory(hass: Any, entry: Any, instance_id: int) -> FakeInstanceClient:
            client = FakeInstanceClient(instance_id)
            clients_by_id[instance_id] = client
            return client

        monkeypatch.setattr(hyperhdr, "_async_create_instance_client", _fake_instance_factory)

        await hyperhdr.async_setup_entry(hass, entry)  # type: ignore[arg-type]

        runtime = entry.runtime_data  # type: ignore[attr-defined]
        assert set(runtime.instance_coordinators) == {0}
        assert clients_by_id[0].start_calls == 1
        assert 1 not in clients_by_id

    async def test_per_instance_connect_failure_does_not_fail_setup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hass = FakeHass()
        entry = _entry()
        _patch_server_client(
            monkeypatch, connect_info={"instance": [{"instance": 0, "friendly_name": "First", "running": True}]}
        )

        async def _boom(hass: Any, entry: Any, instance_id: int) -> FakeInstanceClient:
            raise ConnectionRefusedError("nope")

        monkeypatch.setattr(hyperhdr, "_async_create_instance_client", _boom)

        result = await hyperhdr.async_setup_entry(hass, entry)  # type: ignore[arg-type]

        assert result is True  # setup itself must still succeed
        assert entry.runtime_data.instance_coordinators == {}  # type: ignore[attr-defined]


class TestServerReconnectViaOnConnected:
    async def test_second_on_connected_call_after_setup_reconciles_via_diff_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The SAME on_connected callback wired at setup time is what the
        client's supervisor invokes again on a reconnect after an outage --
        exercise that exact callback a second time (not just the diff
        handler in isolation) to prove the reconnect wiring itself works."""
        hass = FakeHass()
        entry = _entry()
        clients = _patch_server_client(
            monkeypatch,
            connect_info={
                "instance": [
                    {"instance": 1, "friendly_name": "First", "running": True},
                    {"instance": 2, "friendly_name": "Second", "running": True},
                ]
            },
        )

        async def _fake_instance_factory(hass: Any, entry: Any, instance_id: int) -> FakeInstanceClient:
            return FakeInstanceClient(instance_id)

        monkeypatch.setattr(hyperhdr, "_async_create_instance_client", _fake_instance_factory)

        await hyperhdr.async_setup_entry(hass, entry)  # type: ignore[arg-type]
        runtime = entry.runtime_data  # type: ignore[attr-defined]
        assert set(runtime.instance_coordinators) == {1, 2}
        instance2_client = runtime.instance_coordinators[2].client

        # Simulate an outage during which instance 2 was deleted from the
        # server, then a reconnect delivering the reconciled roster.
        server_client = clients[0]
        server_client.sysinfo_result = HyperHdrSysInfo(id="dev", hostname="hyperhdr-host", version="22", build="b")
        await server_client.on_connected({"instance": [{"instance": 1, "friendly_name": "First", "running": True}]})

        assert 2 not in runtime.instance_coordinators
        assert 1 in runtime.instance_coordinators
        assert instance2_client.stop_calls == 1
        assert runtime.server_coordinator.data.instances.keys() == {1}


class TestAsyncUnloadEntry:
    async def test_stops_all_instance_clients_and_server_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hass = FakeHass()
        entry = _entry()
        clients = _patch_server_client(monkeypatch)
        await hyperhdr.async_setup_entry(hass, entry)  # type: ignore[arg-type]

        instance_client = FakeInstanceClient(1)
        coordinator = hyperhdr.HyperHdrInstanceCoordinator(hass, entry, 1)
        coordinator.attach_client(instance_client)  # type: ignore[arg-type]
        entry.runtime_data.instance_coordinators[1] = coordinator  # type: ignore[attr-defined]

        result = await hyperhdr.async_unload_entry(hass, entry)  # type: ignore[arg-type]

        assert result is True
        assert instance_client.stop_calls == 1
        assert clients[0].stop_calls == 1
        assert hass.config_entries.unload_calls == [(entry, [])]

    async def test_idempotent_when_clients_already_stopped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hass = FakeHass()
        entry = _entry()
        clients = _patch_server_client(monkeypatch)
        await hyperhdr.async_setup_entry(hass, entry)  # type: ignore[arg-type]

        await hyperhdr.async_unload_entry(hass, entry)  # type: ignore[arg-type]
        result_again = await hyperhdr.async_unload_entry(hass, entry)  # type: ignore[arg-type]

        assert result_again is True
        assert clients[0].stop_calls == 2  # our fake doesn't guard re-entry; real client.stop() is idempotent

    async def test_unload_before_setup_does_not_raise(self) -> None:
        hass = FakeHass()
        entry = _entry()
        result = await hyperhdr.async_unload_entry(hass, entry)  # type: ignore[arg-type]
        assert result is True
