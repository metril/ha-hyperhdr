"""Pure, HA-flow-independent support code for ``config_flow.py``.

Deliberately free of ``homeassistant.config_entries``/``homeassistant.
data_entry_flow`` imports (the actual ``ConfigFlow``/``OptionsFlow`` step
orchestration) so this module -- connection validation, error mapping,
SSDP location parsing, and voluptuous schema building -- can be unit tested
directly, the same way ``client.py``/``models.py`` are, without hand-rolling
a fake HA ``FlowManager``. The only HA import here is ``homeassistant.
helpers.selector``, a small set of data-shape wrappers with no flow-manager
behavior of their own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import aiohttp
import voluptuous as vol
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client import HyperHdrServerClient
from .const import (
    CONF_ADMIN_PASSWORD,
    CONF_TOKEN,
    CONF_USE_SSL,
    CONF_VERIFY_SSL,
    DEFAULT_HEARTBEAT,
    DEFAULT_PORT,
    DEFAULT_PRIORITY,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_STALE_TIMEOUT,
    OPT_DEFAULT_PRIORITY,
    OPT_HEARTBEAT,
    OPT_HIDDEN_EFFECTS,
    OPT_REQUEST_TIMEOUT,
    OPT_STALE_TIMEOUT,
)
from .exceptions import HyperHdrAuthError, HyperHdrConnectionError, HyperHdrError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .models import HyperHdrSysInfo

_LOGGER = logging.getLogger(__name__)

MIN_PRIORITY = 1
MAX_PRIORITY = 253


# --- connection validation ---------------------------------------------------


@dataclass(slots=True)
class ValidationResult:
    """Outcome of one ``async_validate_connection_with_session`` attempt.

    ``error`` is ``None`` on success (in which case ``sysinfo`` is
    populated) or one of ``"cannot_connect"``/``"invalid_auth"``/
    ``"unknown"`` -- directly usable as ``errors["base"]`` in a config flow
    form. ``token_required`` reflects the server's ``authorize/
    tokenRequired`` answer regardless of whether the overall attempt
    succeeded, so a caller can tell "no token was needed" apart from
    "a token was needed and the one supplied (if any) was wrong/missing".
    """

    error: str | None
    sysinfo: HyperHdrSysInfo | None = None
    token_required: bool = False


async def async_validate_connection_with_session(
    session: Any,
    *,
    host: str,
    port: int,
    use_ssl: bool,
    token: str | None,
    admin_password: str | None,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    heartbeat: float = DEFAULT_HEARTBEAT,
    stale_timeout: float = DEFAULT_STALE_TIMEOUT,
) -> ValidationResult:
    """The HA-independent core of connection validation.

    Builds a throwaway ``HyperHdrServerClient`` bound to ``session`` and
    runs a single ``async_connect_once()`` attempt (no supervisor/retry).
    Never raises -- every failure mode is captured as ``ValidationResult.
    error`` so callers (the config flow's steps) only ever branch on data,
    never on exceptions.

    Note on the except ordering: unlike the supervisor's reconnect loop
    (which treats a raw transport failure -- e.g. ``aiohttp.
    ClientConnectorError``/``ConnectionRefusedError`` from a bad host/port,
    neither of which ``client.py`` wraps into ``HyperHdrConnectionError``
    -- as just another reason to retry), this one-shot validation needs
    those to surface as ``"cannot_connect"`` too, not fall through to the
    generic ``"unknown"`` bucket.
    """
    client = HyperHdrServerClient(
        session,
        host,
        port,
        use_ssl=use_ssl,
        token=token,
        admin_password=admin_password,
        request_timeout=request_timeout,
        heartbeat=heartbeat,
        stale_timeout=stale_timeout,
    )
    try:
        sysinfo = await client.async_connect_once()
    except HyperHdrAuthError:
        return ValidationResult(error="invalid_auth", token_required=client.token_required)
    except (HyperHdrConnectionError, aiohttp.ClientError, OSError, TimeoutError):
        return ValidationResult(error="cannot_connect", token_required=client.token_required)
    except HyperHdrError:
        # Any other protocol-level failure (e.g. HyperHdrApiError from a
        # rejected sysinfo call) -- the flow has no more specific error
        # string for "connected fine but the server then misbehaved".
        return ValidationResult(error="cannot_connect", token_required=client.token_required)
    except Exception:  # noqa: BLE001 - validation must never crash the config flow
        _LOGGER.exception("unexpected error validating HyperHDR connection")
        return ValidationResult(error="unknown", token_required=client.token_required)
    return ValidationResult(error=None, sysinfo=sysinfo, token_required=client.token_required)


# --- SSDP discovery parsing --------------------------------------------------


def _safe_hostname_and_port(url: str) -> tuple[str | None, int | None]:
    """``urlparse(url).hostname``/``.port``, swallowing every way a
    malformed URL can raise -- ``urlparse`` itself for some inputs, but
    ``.port`` also raises ``ValueError`` lazily (e.g. an out-of-range port
    number) only when accessed, not at parse time."""
    try:
        parsed = urlparse(url)
        return parsed.hostname, parsed.port
    except ValueError:
        return None, None


def host_port_from_ssdp(
    ssdp_location: str | None, upnp: Mapping[str, Any], *, default_port: int = DEFAULT_PORT
) -> tuple[str, int] | tuple[None, None]:
    """Best-effort host/port extraction from an SSDP discovery.

    HyperHDR's SSDP payload shape wasn't characterized during recon (see
    ``docs/api-notes.md``), so this stays defensive: the host comes from
    ``ssdp_location``'s netloc; the port comes from that same URL if
    present, else from a ``presentationURL`` in the UPnP description dict
    if present, else falls back to ``default_port``. Returns ``(None,
    None)`` if a host can't be determined at all (caller should abort).
    """
    if not ssdp_location:
        return None, None
    host, port = _safe_hostname_and_port(ssdp_location)
    if not host:
        return None, None

    if port is None:
        presentation_url = upnp.get("presentationURL")
        if isinstance(presentation_url, str):
            _, port = _safe_hostname_and_port(presentation_url)
    return host, port if port is not None else default_port


# --- voluptuous schema builders ----------------------------------------------
#
# Pure functions of plain dict defaults -> vol.Schema. Kept here (not
# inline in config_flow.py's step methods) so their shape -- required vs.
# optional, default values, the exact wire key names -- is unit testable
# without needing a real/stubbed ConfigFlow.


def connection_schema(defaults: Mapping[str, Any] | None = None) -> vol.Schema:
    """host/port/use_ssl/verify_ssl -- used by the ``user``, ``ssdp_confirm``,
    and ``reconfigure`` steps."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=d.get(CONF_HOST, "")): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            ),
            vol.Required(CONF_PORT, default=d.get(CONF_PORT, DEFAULT_PORT)): int,
            vol.Required(CONF_USE_SSL, default=d.get(CONF_USE_SSL, False)): BooleanSelector(),
            vol.Required(CONF_VERIFY_SSL, default=d.get(CONF_VERIFY_SSL, True)): BooleanSelector(),
        }
    )


def token_schema(default: str = "") -> vol.Schema:
    """Single required token field -- used by the ``auth`` step (only
    reached once the server has already confirmed a token is required)."""
    return vol.Schema(
        {
            vol.Required(CONF_TOKEN, default=default): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        }
    )


def admin_schema(default: str = "") -> vol.Schema:
    """Single optional admin-password field -- used by the ``admin`` step."""
    return vol.Schema(
        {
            vol.Optional(CONF_ADMIN_PASSWORD, default=default): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
        }
    )


def reauth_schema(*, token_required: bool, token_default: str = "", admin_default: str = "") -> vol.Schema:
    """token + admin_password -- used by ``reauth_confirm``. The token
    field is ``Required`` only when the server currently demands one;
    otherwise ``Optional`` (the entry may only need a fresh admin
    password)."""
    token_key: vol.Marker = (
        vol.Required(CONF_TOKEN, default=token_default)
        if token_required
        else vol.Optional(CONF_TOKEN, default=token_default)
    )
    return vol.Schema(
        {
            token_key: TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            vol.Optional(CONF_ADMIN_PASSWORD, default=admin_default): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
        }
    )


def options_schema(current: Mapping[str, Any], effect_options: list[str]) -> vol.Schema:
    """default_priority / hidden_effects / request_timeout / heartbeat /
    stale_timeout -- the options flow's single form.

    ``effect_options`` is the multi-select's choice list; any effect name
    already stored in ``current``'s ``hidden_effects`` is unioned in even
    if it isn't currently known (e.g. the entry isn't loaded/connected right
    now), so a previously saved selection is never silently dropped from
    the form.
    """
    stored_hidden = list(current.get(OPT_HIDDEN_EFFECTS, []))
    all_options = sorted({*effect_options, *stored_hidden})
    return vol.Schema(
        {
            vol.Required(
                OPT_DEFAULT_PRIORITY, default=current.get(OPT_DEFAULT_PRIORITY, DEFAULT_PRIORITY)
            ): NumberSelector(NumberSelectorConfig(min=MIN_PRIORITY, max=MAX_PRIORITY, mode=NumberSelectorMode.BOX)),
            vol.Optional(OPT_HIDDEN_EFFECTS, default=stored_hidden): SelectSelector(
                SelectSelectorConfig(options=all_options, multiple=True, mode=SelectSelectorMode.DROPDOWN)
            ),
            vol.Required(
                OPT_REQUEST_TIMEOUT, default=current.get(OPT_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT)
            ): NumberSelector(NumberSelectorConfig(min=1, max=120, step=0.5, mode=NumberSelectorMode.BOX)),
            vol.Required(OPT_HEARTBEAT, default=current.get(OPT_HEARTBEAT, DEFAULT_HEARTBEAT)): NumberSelector(
                NumberSelectorConfig(min=5, max=300, step=1, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(
                OPT_STALE_TIMEOUT, default=current.get(OPT_STALE_TIMEOUT, DEFAULT_STALE_TIMEOUT)
            ): NumberSelector(NumberSelectorConfig(min=10, max=600, step=1, mode=NumberSelectorMode.BOX)),
        }
    )
