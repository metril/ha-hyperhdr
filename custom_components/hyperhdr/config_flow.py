"""Config flow for the HyperHDR integration.

Manual entry (host/port/use_ssl/verify_ssl, optional token, optional admin
password) and SSDP discovery converge on the same connection-validation
path; reauth re-collects credentials, reconfigure re-collects connection
details, and options exposes the runtime tunables from const.py's ``OPT_*``
keys.

The actual voluptuous schema shapes, the SSDP location parser, and the
connect-and-classify-the-outcome logic live in ``flow_support.py`` (no
``homeassistant.config_entries``/``data_entry_flow`` imports there) so they
can be unit tested directly. This module is the thin HA-flow-orchestration
glue on top -- per project convention (see ha-vsphere's own config_flow.py),
it is exercised by the live-HA validation checklist rather than a stubbed
``FlowManager``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_ADMIN_PASSWORD, CONF_TOKEN, CONF_USE_SSL, CONF_VERIFY_SSL, DEFAULT_PORT, DOMAIN
from .flow_support import (
    ValidationResult,
    admin_schema,
    async_validate_connection_with_session,
    connection_schema,
    host_port_from_ssdp,
    options_schema,
    reauth_schema,
    token_schema,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo

    from .models import HyperHdrSysInfo

_LOGGER = logging.getLogger(__name__)


def _session_for_flow(hass: HomeAssistant, data: Mapping[str, Any]) -> Any:
    """Mirror ``__init__.py``'s ``_get_session`` for flow-time data -- no
    ``ConfigEntry`` exists yet to read ``use_ssl``/``verify_ssl`` off of."""
    use_ssl = bool(data.get(CONF_USE_SSL, False))
    verify_ssl = data.get(CONF_VERIFY_SSL, True)
    if use_ssl and not verify_ssl:
        return async_get_clientsession(hass, verify_ssl=False)
    return async_get_clientsession(hass)


async def _async_validate_connection(hass: HomeAssistant, data: Mapping[str, Any]) -> ValidationResult:
    """Shared single-attempt validation for every step that needs one --
    resolves ``data`` to an aiohttp session via ``hass``, then delegates to
    the pure core in ``flow_support.py``."""
    session = _session_for_flow(hass, data)
    return await async_validate_connection_with_session(
        session,
        host=data[CONF_HOST],
        port=data.get(CONF_PORT, DEFAULT_PORT),
        use_ssl=bool(data.get(CONF_USE_SSL, False)),
        token=data.get(CONF_TOKEN),
        admin_password=data.get(CONF_ADMIN_PASSWORD),
    )


def _effect_options(entry: ConfigEntry) -> list[str]:
    """Union of known effect names across every loaded instance
    coordinator, for the options flow's hidden-effects multi-select.

    Defensive: the entry may not be loaded (setup failed, reauth pending,
    or the options flow was opened before first setup finished) when this
    runs, in which case ``runtime_data`` doesn't exist yet -- mirrors the
    ``hasattr(entry, "runtime_data")`` guard used throughout __init__.py.
    """
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return []
    names: set[str] = set()
    for coordinator in runtime.instance_coordinators.values():
        if coordinator.data is not None:
            names.update(effect.name for effect in coordinator.data.effects)
    return sorted(names)


class HyperHdrConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    # mypy doesn't have the real homeassistant.config_entries.ConfigFlow
    # available here (this project intentionally doesn't install the full
    # homeassistant package -- see const.py's own comment on CONF_HOST/
    # CONF_PORT for the same reasoning); with ignore_missing_imports
    # treating ConfigFlow as Any, mypy falls back to checking
    # __init_subclass__ against plain object's signature, which doesn't
    # accept the `domain=` kwarg HA's real ConfigFlow does. Verified live
    # against installed HA (see docs/api-notes.md-adjacent Phase 4 report)
    # that this is exactly the real, supported subclassing pattern.
    """Config flow for HyperHDR: manual entry or SSDP discovery, optional
    token + optional admin password, reauth, reconfigure, options."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict[str, Any] = {}
        self._sysinfo: HyperHdrSysInfo | None = None

    # --- user / ssdp_confirm: shared connection form + validation --------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial host/port/use_ssl/verify_ssl step."""
        return await self._async_connection_step("user", user_input)

    async def async_step_ssdp_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm (and allow editing) the connection details an SSDP
        discovery pre-filled, then validate exactly like ``user``."""
        return await self._async_connection_step("ssdp_confirm", user_input)

    async def _async_connection_step(self, step_id: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        """Shared submit-and-validate logic for ``user``/``ssdp_confirm``:
        both show the same connection form and branch identically on the
        validation outcome (cannot_connect/unknown -> re-show with error;
        token required -> ``auth``; success -> ``_finish_setup_flow``)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await _async_validate_connection(self.hass, user_input)
            if result.error is None:
                self._data = dict(user_input)
                assert result.sysinfo is not None  # guaranteed by ValidationResult when error is None
                return await self._finish_setup_flow(result.sysinfo)
            if result.token_required:
                self._data = dict(user_input)
                return await self.async_step_auth()
            errors["base"] = result.error

        return self.async_show_form(
            step_id=step_id,
            data_schema=connection_schema(user_input if user_input is not None else self._data),
            errors=errors,
        )

    async def _finish_setup_flow(self, sysinfo: HyperHdrSysInfo) -> ConfigFlowResult:
        """Common tail for every path that just obtained a valid connection
        + sysinfo: pin the entry's unique id to the server's own id, then
        move on to the (optional) admin-password step."""
        if sysinfo.id:
            await self.async_set_unique_id(sysinfo.id)
            self._abort_if_unique_id_configured(updates={CONF_HOST: self._data[CONF_HOST]})
        self._sysinfo = sysinfo
        return await self.async_step_admin()

    # --- auth: token (only reached once the server has confirmed one is
    # required) ------------------------------------------------------------

    async def async_step_auth(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect an API token. No requestToken UI flow -- YAGNI'd for v1;
        create a token in HyperHDR's Network Services panel and paste it
        here (see strings.json's data_description)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = {**self._data, **user_input}
            result = await _async_validate_connection(self.hass, candidate)
            if result.error is None:
                self._data = candidate
                assert result.sysinfo is not None
                return await self._finish_setup_flow(result.sysinfo)
            errors["base"] = result.error

        return self.async_show_form(step_id="auth", data_schema=token_schema(), errors=errors)

    # --- admin: optional admin password, then create the entry -----------

    async def async_step_admin(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect an optional admin password (enables instance start/stop
        and other admin-gated features). Leaving it blank skips validation
        entirely and creates the entry as-is."""
        errors: dict[str, str] = {}
        if user_input is not None:
            admin_password = user_input.get(CONF_ADMIN_PASSWORD) or None
            if admin_password:
                candidate = {**self._data, CONF_ADMIN_PASSWORD: admin_password}
                result = await _async_validate_connection(self.hass, candidate)
                if result.error is not None:
                    errors["base"] = result.error
                else:
                    self._data = candidate
            if not errors:
                return self._async_create_entry_from_state()

        return self.async_show_form(step_id="admin", data_schema=admin_schema(), errors=errors)

    def _async_create_entry_from_state(self) -> ConfigFlowResult:
        sysinfo = self._sysinfo
        title = sysinfo.hostname if sysinfo and sysinfo.hostname else str(self._data.get(CONF_HOST, DOMAIN))
        return self.async_create_entry(title=title, data=self._data)

    # --- ssdp discovery ----------------------------------------------------

    async def async_step_ssdp(self, discovery_info: SsdpServiceInfo) -> ConfigFlowResult:
        """Handle SSDP discovery: pre-fill the connection form from the
        discovery's location URL, then hand off to ``ssdp_confirm``."""
        host, port = host_port_from_ssdp(discovery_info.ssdp_location, discovery_info.upnp)
        if host is None or port is None:
            return self.async_abort(reason="cannot_connect")

        # Best-effort early dedup keyed off the UPnP UDN -- NOT the same
        # namespace as the entry's real unique_id (the server's own
        # sysinfo id, set authoritatively in `_finish_setup_flow`), so this
        # is purely a fast-path: it can only help (skip a redundant
        # confirm+connect for an obvious repeat announcement of an
        # already-configured device), never wrongly abort a legitimate new
        # device, since a UDN string can't collide with a sysinfo-id
        # unique_id already stored for a different entry.
        udn = discovery_info.ssdp_udn
        if udn:
            await self.async_set_unique_id(udn)
            self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})

        self._data = {CONF_HOST: host, CONF_PORT: port, CONF_USE_SSL: False, CONF_VERIFY_SSL: True}
        friendly_name = discovery_info.upnp.get("friendlyName") or host
        self.context["title_placeholders"] = {"name": str(friendly_name)}
        return await self.async_step_ssdp_confirm()

    # --- reauth --------------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Entry point for a reauth flow started by ``entry.async_start_reauth``."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Re-prompt for token and/or admin password. The token field is
        shown as required only if the server currently demands one;
        otherwise both fields are optional (only a stale admin password may
        need refreshing)."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            updates: dict[str, Any] = {}
            token = user_input.get(CONF_TOKEN) or None
            admin_password = user_input.get(CONF_ADMIN_PASSWORD) or None
            if token:
                updates[CONF_TOKEN] = token
            if admin_password:
                updates[CONF_ADMIN_PASSWORD] = admin_password
            candidate = {**reauth_entry.data, **updates}

            result = await _async_validate_connection(self.hass, candidate)
            if result.error is None:
                return self.async_update_reload_and_abort(reauth_entry, data_updates=updates)
            errors["base"] = result.error

        token_required = await self._async_probe_token_required(reauth_entry.data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=reauth_schema(
                token_required=token_required, token_default=str(reauth_entry.data.get(CONF_TOKEN, ""))
            ),
            errors=errors,
        )

    async def _async_probe_token_required(self, existing_data: Mapping[str, Any]) -> bool:
        """Best-effort read of whether the server currently demands a
        token, purely to decide how to label the reauth form -- the real
        validation happens on submit. If the probe itself can't connect,
        default to ``True`` (show the token field) since that's the safer
        of the two wrong guesses."""
        probe_data = {k: v for k, v in existing_data.items() if k not in (CONF_TOKEN, CONF_ADMIN_PASSWORD)}
        result = await _async_validate_connection(self.hass, probe_data)
        if result.error == "cannot_connect":
            return True
        return result.token_required

    # --- reconfigure -----------------------------------------------------

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Update host/port/use_ssl/verify_ssl for an existing entry.
        Aborts ``wrong_device`` if the new address answers as a different
        HyperHDR server (mismatched sysinfo id)."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            candidate = {**reconfigure_entry.data, **user_input}
            result = await _async_validate_connection(self.hass, candidate)
            if result.error is None:
                assert result.sysinfo is not None
                if result.sysinfo.id:
                    await self.async_set_unique_id(result.sysinfo.id)
                    self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(reconfigure_entry, data_updates=user_input)
            errors["base"] = result.error

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=connection_schema(reconfigure_entry.data),
            errors=errors,
        )

    # --- options -----------------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HyperHdrOptionsFlow:
        """Return the options flow handler.

        ``config_entry`` is unused directly: modern ``OptionsFlow`` derives
        ``self.config_entry`` as a property (populated by the flow manager
        from ``self.hass``/``self.handler``) rather than taking it in
        ``__init__`` -- the pattern the now-deprecated
        ``OptionsFlowWithConfigEntry`` used.
        """
        return HyperHdrOptionsFlow()


class HyperHdrOptionsFlow(OptionsFlow):
    """Single-form options: default priority, hidden effects, timeouts.

    Plain ``OptionsFlow`` deliberately -- not ``OptionsFlowWithReload``
    (which explicitly forbids coexisting with a config entry update
    listener, and __init__.py already has one that reloads on options
    change) and not the deprecated ``OptionsFlowWithConfigEntry``.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show (and, on submit, save) the single options form."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=options_schema(self.config_entry.options, _effect_options(self.config_entry)),
        )
