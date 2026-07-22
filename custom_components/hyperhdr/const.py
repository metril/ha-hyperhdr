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
WATCHDOG_INTERVAL = 15.0

# Ledstream/imagestream push topics. These reuse the *request's* tan on every
# frame (not a fresh/incrementing tan) and key their payload "result", unlike
# every other `-update` push topic (which use "data") -- see docs/api-notes.md.
LEDSTREAM_UPDATE_TOPIC = "ledcolors-ledstream-update"
IMAGESTREAM_UPDATE_TOPIC = "ledcolors-imagestream-update"

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

# Instance-scoped push topics with a matching HyperHdrInstanceData.apply_*
# handler (see coordinator.py). Named here (rather than left as inline
# literals) since both coordinator.py and __init__.py need to reference the
# exact wire strings.
COMPONENTS_UPDATE_TOPIC = "components-update"
PRIORITIES_UPDATE_TOPIC = "priorities-update"
ADJUSTMENT_UPDATE_TOPIC = "adjustment-update"
EFFECTS_UPDATE_TOPIC = "effects-update"

# Server-scoped push topic carrying the instance roster.
INSTANCE_UPDATE_TOPIC = "instance-update"

# Dispatcher signals for dynamic entity dispatch (fired as f"{SIGNAL}_{entry.entry_id}").
# SIGNAL_INSTANCE_ADDED: a new instance id appeared in the roster (regardless of running state).
SIGNAL_INSTANCE_ADDED = f"{DOMAIN}_instance_added"
# SIGNAL_INSTANCE_READY: an instance coordinator was just created and seeded -- entities for
# that instance can now be added.
SIGNAL_INSTANCE_READY = f"{DOMAIN}_instance_ready"
