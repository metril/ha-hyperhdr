"""Tests for the pure config-flow support code in flow_support.py:
voluptuous schema shape, SSDP location parsing, and the connection
validation helper's exception -> error-code mapping (against a real
``HyperHdrServerClient`` + scripted fake websocket -- not a mock).
"""

from __future__ import annotations

import voluptuous as vol
from conftest import (
    FakeClientSession,
    FakeWebSocket,
    load_fixture,
    login_failure_frame,
    login_success_frame,
    sysinfo_frame,
    token_required_frame,
)

from custom_components.hyperhdr import flow_support
from custom_components.hyperhdr.const import (
    CONF_ADMIN_PASSWORD,
    CONF_TOKEN,
    DEFAULT_PORT,
    DEFAULT_PRIORITY,
    OPT_DEFAULT_PRIORITY,
    OPT_HEARTBEAT,
    OPT_HIDDEN_EFFECTS,
    OPT_REQUEST_TIMEOUT,
    OPT_STALE_TIMEOUT,
)


def _schema_dict(schema: vol.Schema) -> dict[str, vol.Marker]:
    """Key a schema's markers by their plain string name for easy lookup."""
    return {str(marker): marker for marker in schema.schema}


class TestConnectionSchema:
    def test_defaults_and_required_keys(self) -> None:
        markers = _schema_dict(flow_support.connection_schema())
        assert set(markers) == {"host", "port", "use_ssl", "verify_ssl"}
        assert all(isinstance(m, vol.Required) for m in markers.values())
        assert markers["host"].default() == ""
        assert markers["port"].default() == DEFAULT_PORT
        assert markers["use_ssl"].default() is False
        assert markers["verify_ssl"].default() is True

    def test_defaults_pulled_from_existing_values(self) -> None:
        markers = _schema_dict(
            flow_support.connection_schema({"host": "10.0.0.5", "port": 1234, "use_ssl": True, "verify_ssl": False})
        )
        assert markers["host"].default() == "10.0.0.5"
        assert markers["port"].default() == 1234
        assert markers["use_ssl"].default() is True
        assert markers["verify_ssl"].default() is False


class TestTokenAndAdminSchema:
    def test_token_schema_is_required(self) -> None:
        markers = _schema_dict(flow_support.token_schema())
        assert set(markers) == {CONF_TOKEN}
        assert isinstance(markers[CONF_TOKEN], vol.Required)
        assert markers[CONF_TOKEN].default() == ""

    def test_admin_schema_is_optional(self) -> None:
        markers = _schema_dict(flow_support.admin_schema())
        assert set(markers) == {CONF_ADMIN_PASSWORD}
        assert isinstance(markers[CONF_ADMIN_PASSWORD], vol.Optional)
        assert not isinstance(markers[CONF_ADMIN_PASSWORD], vol.Required)


class TestReauthSchema:
    def test_token_required_true_makes_token_field_required(self) -> None:
        markers = _schema_dict(flow_support.reauth_schema(token_required=True))
        assert isinstance(markers[CONF_TOKEN], vol.Required)

    def test_token_required_false_makes_token_field_optional(self) -> None:
        markers = _schema_dict(flow_support.reauth_schema(token_required=False))
        assert isinstance(markers[CONF_TOKEN], vol.Optional)
        assert not isinstance(markers[CONF_TOKEN], vol.Required)

    def test_admin_password_always_optional(self) -> None:
        for token_required in (True, False):
            markers = _schema_dict(flow_support.reauth_schema(token_required=token_required))
            assert isinstance(markers[CONF_ADMIN_PASSWORD], vol.Optional)


class TestOptionsSchema:
    def test_defaults_from_const_when_no_current_options(self) -> None:
        markers = _schema_dict(flow_support.options_schema({}, []))
        assert markers[OPT_DEFAULT_PRIORITY].default() == DEFAULT_PRIORITY
        assert markers[OPT_HIDDEN_EFFECTS].default() == []

    def test_current_options_override_defaults(self) -> None:
        current = {
            OPT_DEFAULT_PRIORITY: 200,
            OPT_HIDDEN_EFFECTS: ["Rainbow swirl fast"],
            OPT_REQUEST_TIMEOUT: 5,
            OPT_HEARTBEAT: 20,
            OPT_STALE_TIMEOUT: 60,
        }
        markers = _schema_dict(flow_support.options_schema(current, ["Rainbow swirl fast", "Knight rider"]))
        assert markers[OPT_DEFAULT_PRIORITY].default() == 200
        assert markers[OPT_HIDDEN_EFFECTS].default() == ["Rainbow swirl fast"]
        assert markers[OPT_REQUEST_TIMEOUT].default() == 5
        assert markers[OPT_HEARTBEAT].default() == 20
        assert markers[OPT_STALE_TIMEOUT].default() == 60

    def test_stored_hidden_effect_survives_even_if_not_currently_known(self) -> None:
        """Regression guard: an entry that isn't loaded (so no live effect
        list is available) must not silently drop a previously hidden
        effect from the selectable options -- see options_schema's
        docstring."""
        current = {OPT_HIDDEN_EFFECTS: ["Some effect no longer reported live"]}
        schema = flow_support.options_schema(current, [])
        markers = _schema_dict(schema)
        select_selector = schema.schema[markers[OPT_HIDDEN_EFFECTS]]
        assert "Some effect no longer reported live" in select_selector.config["options"]


class TestHostPortFromSsdp:
    def test_location_with_explicit_port(self) -> None:
        assert flow_support.host_port_from_ssdp("http://192.168.1.50:8090/description.xml", {}) == (
            "192.168.1.50",
            8090,
        )

    def test_missing_location_returns_none_none(self) -> None:
        assert flow_support.host_port_from_ssdp(None, {}) == (None, None)
        assert flow_support.host_port_from_ssdp("", {}) == (None, None)

    def test_location_without_port_falls_back_to_default(self) -> None:
        assert flow_support.host_port_from_ssdp("http://192.168.1.50/description.xml", {}) == (
            "192.168.1.50",
            DEFAULT_PORT,
        )

    def test_location_without_port_uses_presentation_url_port(self) -> None:
        host, port = flow_support.host_port_from_ssdp(
            "http://192.168.1.50/description.xml", {"presentationURL": "http://192.168.1.50:8091/"}
        )
        assert (host, port) == ("192.168.1.50", 8091)

    def test_malformed_location_returns_none_none(self) -> None:
        # A location with an out-of-range port is what actually trips
        # urlparse's port validation (raises ValueError lazily, on access).
        assert flow_support.host_port_from_ssdp("http://192.168.1.50:999999/", {}) == (None, None)


class TestAsyncValidateConnectionWithSession:
    async def test_success_returns_sysinfo_and_no_error(self) -> None:
        sysinfo_info = load_fixture("sysinfo.json")["info"]
        ws = FakeWebSocket([token_required_frame(1, False), sysinfo_frame(2, sysinfo_info)])
        session = FakeClientSession(ws)

        result = await flow_support.async_validate_connection_with_session(
            session, host="10.0.0.5", port=8090, use_ssl=False, token=None, admin_password=None
        )

        assert result.error is None
        assert result.token_required is False
        assert result.sysinfo is not None
        assert result.sysinfo.hostname == "a07b20766d71"

    async def test_token_required_but_missing_is_invalid_auth(self) -> None:
        ws = FakeWebSocket([token_required_frame(1, True)])
        session = FakeClientSession(ws)

        result = await flow_support.async_validate_connection_with_session(
            session, host="10.0.0.5", port=8090, use_ssl=False, token=None, admin_password=None
        )

        assert result.error == "invalid_auth"
        assert result.token_required is True
        assert result.sysinfo is None

    async def test_wrong_token_is_invalid_auth(self) -> None:
        ws = FakeWebSocket([token_required_frame(1, True), login_failure_frame(2)])
        session = FakeClientSession(ws)

        result = await flow_support.async_validate_connection_with_session(
            session, host="10.0.0.5", port=8090, use_ssl=False, token="bad-token", admin_password=None
        )

        assert result.error == "invalid_auth"
        assert result.token_required is True

    async def test_good_token_proceeds_to_sysinfo(self) -> None:
        sysinfo_info = load_fixture("sysinfo.json")["info"]
        ws = FakeWebSocket([token_required_frame(1, True), login_success_frame(2), sysinfo_frame(3, sysinfo_info)])
        session = FakeClientSession(ws)

        result = await flow_support.async_validate_connection_with_session(
            session, host="10.0.0.5", port=8090, use_ssl=False, token="good-token", admin_password=None
        )

        assert result.error is None
        assert result.token_required is True
        assert result.sysinfo is not None

    async def test_wrong_admin_password_is_invalid_auth(self) -> None:
        ws = FakeWebSocket([token_required_frame(1, False), login_failure_frame(2, error="No Authorization")])
        session = FakeClientSession(ws)

        result = await flow_support.async_validate_connection_with_session(
            session, host="10.0.0.5", port=8090, use_ssl=False, token=None, admin_password="wrong"
        )

        assert result.error == "invalid_auth"

    async def test_transport_failure_is_cannot_connect_not_unknown(self) -> None:
        """Regression guard: a raw connect-time exception (e.g. connection
        refused for a wrong port) must map to cannot_connect, not fall
        through to the generic unknown bucket -- client.py never wraps
        ws_connect() failures into HyperHdrConnectionError (the supervisor
        retries on any bare Exception), so this helper has to catch the
        transport-level exception types itself."""
        session = FakeClientSession(ConnectionRefusedError("nope"))

        result = await flow_support.async_validate_connection_with_session(
            session, host="10.0.0.5", port=9999, use_ssl=False, token=None, admin_password=None
        )

        assert result.error == "cannot_connect"

    async def test_disconnect_mid_handshake_is_cannot_connect(self) -> None:
        from conftest import DISCONNECT

        ws = FakeWebSocket([DISCONNECT])
        session = FakeClientSession(ws)

        result = await flow_support.async_validate_connection_with_session(
            session, host="10.0.0.5", port=8090, use_ssl=False, token=None, admin_password=None
        )

        assert result.error == "cannot_connect"
