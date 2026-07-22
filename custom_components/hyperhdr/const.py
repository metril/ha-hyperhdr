"""Constants for the HyperHDR integration."""

from __future__ import annotations

DOMAIN = "hyperhdr"
MANUFACTURER = "HyperHDR"

# Configuration keys.
# Host and port come from homeassistant.const at use sites — do not redefine.
CONF_USE_SSL = "use_ssl"
CONF_VERIFY_SSL = "verify_ssl"
CONF_TOKEN = "token"
CONF_ADMIN_PASSWORD = "admin_password"

# Defaults.
DEFAULT_PORT = 8090
DEFAULT_PRIORITY = 128
DEFAULT_ORIGIN = "Home Assistant"
DEFAULT_REQUEST_TIMEOUT = 10.0
DEFAULT_HEARTBEAT = 30.0
DEFAULT_STALE_TIMEOUT = 90.0
RECONNECT_BASE_DELAY = 2.0
RECONNECT_MAX_DELAY = 60.0

# Options keys.
OPT_DEFAULT_PRIORITY = "default_priority"
OPT_HIDDEN_EFFECTS = "hidden_effects"
OPT_REQUEST_TIMEOUT = "request_timeout"
OPT_HEARTBEAT = "heartbeat"
OPT_STALE_TIMEOUT = "stale_timeout"

# WebSocket subscription topics.
SUBSCRIPTIONS = (
    "components-update",
    "priorities-update",
    "adjustment-update",
    "effects-update",
    "instance-update",
    "videomode-update",
    "settings-update",
)
SERVER_SUBSCRIPTIONS = ("instance-update",)

# Component ids.
COMPONENT_ALL = "ALL"
COMPONENT_LEDDEVICE = "LEDDEVICE"
COMPONENT_HDR = "HDR"

# Known v22 component ids mapped to friendly labels. Component switches are
# built dynamically from serverinfo; this map only provides labels/icons for
# known ids.
COMPONENT_LABELS: dict[str, str] = {
    "ALL": "LED output",
    "HDR": "HDR tone mapping",
    "SMOOTHING": "Smoothing",
    "BLACKBORDER": "Blackborder detection",
    "FORWARDER": "Forwarder",
    "VIDEOGRABBER": "USB capture",
    "SYSTEMGRABBER": "Screen capture",
    "LEDDEVICE": "LED device",
}

HDR_MODE_OFF = 0
HDR_MODE_ON = 1

# Dispatcher signal used later for dynamic entity dispatch.
SIGNAL_INSTANCE_ADDED = f"{DOMAIN}_instance_added"
