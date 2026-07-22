"""Tests for the connect-time auth handshake: tokenRequired/login/admin."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from conftest import FakeClientSession, FakeWebSocket, build_connect_script, wait_until

from custom_components.hyperhdr.client import HyperHdrInstanceClient, HyperHdrServerClient
from custom_components.hyperhdr.exceptions import HyperHdrApiError, HyperHdrAuthError


class TestNoTokenRequired:
    async def test_proceeds_without_token_when_not_required(self) -> None:
        ws = FakeWebSocket(build_connect_script(token_required=False))
        session = FakeClientSession(ws)
        client = HyperHdrServerClient(session, "localhost", 8090)  # type: ignore[arg-type]
        connected_infos: list[dict[str, Any]] = []
        client.on_connected = connected_infos.append

        await client.start()
        await wait_until(lambda: client.connected)
        assert len(connected_infos) == 1
        await client.stop()
        assert not client.connected


class TestTokenRequired:
    async def test_no_token_raises_auth_error_and_fires_on_auth_failed_and_stops_supervisor(self) -> None:
        ws = FakeWebSocket(build_connect_script(token_required=True, token=None))
        session = FakeClientSession(ws)
        client = HyperHdrServerClient(session, "localhost", 8090)  # type: ignore[arg-type]
        auth_failed_count = 0

        def _on_auth_failed() -> None:
            nonlocal auth_failed_count
            auth_failed_count += 1

        client.on_auth_failed = _on_auth_failed

        await client.start()
        await wait_until(lambda: auth_failed_count == 1)
        assert not client.connected

        # Supervisor must have stopped retrying -- no second ws_connect call
        # should ever happen since only one scripted socket exists and no
        # error was raised about running out of sockets.
        await wait_until(lambda: client._supervisor_task is not None and client._supervisor_task.done())
        assert len(session.ws_connect_calls) == 1
        await client.stop()

    async def test_token_present_sends_login_and_succeeds(self) -> None:
        ws = FakeWebSocket(build_connect_script(token_required=True, token="good-token"))
        session = FakeClientSession(ws)
        client = HyperHdrServerClient(session, "localhost", 8090, token="good-token")  # type: ignore[arg-type]

        await client.start()
        await wait_until(lambda: client.connected)

        login_messages = [m for m in ws.sent if m.get("subcommand") == "login"]
        assert len(login_messages) == 1
        assert login_messages[0]["token"] == "good-token"
        assert "password" not in login_messages[0]
        await client.stop()

    async def test_token_login_failure_raises_auth_error(self) -> None:
        ws = FakeWebSocket(build_connect_script(token_required=True, token="bad-token", token_login_success=False))
        session = FakeClientSession(ws)
        client = HyperHdrServerClient(session, "localhost", 8090, token="bad-token")  # type: ignore[arg-type]
        auth_failed_count = 0
        client.on_auth_failed = lambda: None

        def _mark() -> None:
            nonlocal auth_failed_count
            auth_failed_count += 1

        client.on_auth_failed = _mark

        await client.start()
        await wait_until(lambda: auth_failed_count == 1)
        assert not client.connected
        await client.stop()


class TestAdminPassword:
    async def test_admin_password_sends_password_login_and_sets_admin_logged_in(self) -> None:
        ws = FakeWebSocket(build_connect_script(admin_password="hyperhdr"))
        session = FakeClientSession(ws)
        client = HyperHdrServerClient(session, "localhost", 8090, admin_password="hyperhdr")  # type: ignore[arg-type]

        await client.start()
        await wait_until(lambda: client.connected)

        login_messages = [m for m in ws.sent if m.get("subcommand") == "login"]
        assert len(login_messages) == 1
        assert login_messages[0]["password"] == "hyperhdr"
        assert "token" not in login_messages[0]
        assert client.admin_logged_in is True
        await client.stop()

    async def test_admin_password_failure_raises_auth_error(self) -> None:
        ws = FakeWebSocket(build_connect_script(admin_password="wrong", admin_login_success=False))
        session = FakeClientSession(ws)
        client = HyperHdrServerClient(session, "localhost", 8090, admin_password="wrong")  # type: ignore[arg-type]
        auth_failed_count = 0
        client.on_auth_failed = lambda: None

        def _mark() -> None:
            nonlocal auth_failed_count
            auth_failed_count += 1

        client.on_auth_failed = _mark

        await client.start()
        await wait_until(lambda: auth_failed_count == 1)
        assert client.admin_logged_in is False
        await client.stop()

    async def test_start_instance_without_admin_login_raises_without_sending(self) -> None:
        ws = FakeWebSocket(build_connect_script())
        session = FakeClientSession(ws)
        client = HyperHdrServerClient(session, "localhost", 8090)  # type: ignore[arg-type]

        await client.start()
        await wait_until(lambda: client.connected)
        assert client.admin_logged_in is False

        sent_before = len(ws.sent)
        with pytest.raises(HyperHdrAuthError):
            await client.start_instance(1)
        assert len(ws.sent) == sent_before  # nothing was sent
        await client.stop()


class TestTokenNeverLogged:
    async def test_token_never_appears_in_any_log_record(self, caplog: pytest.LogCaptureFixture) -> None:
        secret_token = "super-secret-token-value-xyz"
        ws = FakeWebSocket(build_connect_script(token_required=True, token=secret_token))
        session = FakeClientSession(ws)
        client = HyperHdrServerClient(session, "localhost", 8090, token=secret_token)  # type: ignore[arg-type]

        with caplog.at_level(logging.DEBUG):
            await client.start()
            await wait_until(lambda: client.connected)
            await client.stop()

        for record in caplog.records:
            assert secret_token not in record.getMessage()

    async def test_admin_password_never_appears_in_any_log_record(self, caplog: pytest.LogCaptureFixture) -> None:
        secret_password = "super-secret-admin-password"
        ws = FakeWebSocket(build_connect_script(admin_password=secret_password))
        session = FakeClientSession(ws)
        client = HyperHdrServerClient(session, "localhost", 8090, admin_password=secret_password)  # type: ignore[arg-type]

        with caplog.at_level(logging.DEBUG):
            await client.start()
            await wait_until(lambda: client.connected)
            await client.stop()

        for record in caplog.records:
            assert secret_password not in record.getMessage()


class TestInstanceSwitchToAuth:
    """switchTo does NOT require an admin login on live HyperHDR 22.0.0beta2
    (verified against hyperhdr-dev) -- only a genuine auth failure with no
    admin password configured is converted to HyperHdrAuthError."""

    async def test_switch_to_success_without_admin_password(self) -> None:
        ws = FakeWebSocket(build_connect_script(is_instance=True, instance_id=2))
        session = FakeClientSession(ws)
        client = HyperHdrInstanceClient(session, "localhost", 8090, instance_id=2)  # type: ignore[arg-type]
        await client.start()
        await wait_until(lambda: client.connected)

        switch_messages = [m for m in ws.sent if m.get("subcommand") == "switchTo"]
        assert len(switch_messages) == 1
        assert switch_messages[0]["instance"] == 2
        await client.stop()

    async def test_switch_to_auth_failure_without_admin_password_raises_auth_error(self) -> None:
        ws = FakeWebSocket(build_connect_script(is_instance=True, instance_id=1, switch_to_success=False))
        session = FakeClientSession(ws)
        client = HyperHdrInstanceClient(session, "localhost", 8090, instance_id=1)  # type: ignore[arg-type]
        auth_failed_count = 0

        def _mark() -> None:
            nonlocal auth_failed_count
            auth_failed_count += 1

        client.on_auth_failed = _mark

        await client.start()
        await wait_until(lambda: auth_failed_count == 1)
        assert not client.connected
        await client.stop()

    async def test_switch_to_failure_with_admin_password_set_is_not_converted(self) -> None:
        # If an admin password WAS configured (and its login already
        # succeeded, or this branch would never be reached), a switchTo
        # "No Authorization" failure is NOT actionable via reauth -- it
        # must surface as the original HyperHdrApiError, not be silently
        # reinterpreted as an auth error. Call _connect_once() directly
        # (bypassing the supervisor's broad except Exception) to assert
        # the precise exception type.
        ws = FakeWebSocket(
            build_connect_script(admin_password="hyperhdr", is_instance=True, instance_id=3, switch_to_success=False)
        )
        session = FakeClientSession(ws)
        client = HyperHdrInstanceClient(
            session,  # type: ignore[arg-type]
            "localhost",
            8090,
            instance_id=3,
            admin_password="hyperhdr",
        )

        with pytest.raises(HyperHdrApiError) as excinfo:
            await client._connect_once()
        assert not isinstance(excinfo.value, HyperHdrAuthError)
        assert excinfo.value.error == "No Authorization"
