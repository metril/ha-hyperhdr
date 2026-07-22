<p align="center">
  <img src="https://raw.githubusercontent.com/metril/ha-hyperhdr/main/custom_components/hyperhdr/brand/logo.png" alt="HyperHDR" width="380">
</p>

# HyperHDR for Home Assistant

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)

A zero-dependency, push-based Home Assistant integration for [HyperHDR](https://github.com/awawa-dev/HyperHDR) ambient lighting. The integration talks to HyperHDR's own WebSocket JSON-RPC API directly (no MQTT, no add-on, no polling) and supports multiple HyperHDR instances on the same server as independent devices. Built for HyperHDR v20+; verified live against v22.

## Features

- **Light** — one light entity per instance with RGB color, brightness (via `luminanceGain`), and effect selection (plus a `Solid` pseudo-effect for a plain color)
- **Component switches** — one switch per HyperHDR component reported by the server (LED output, HDR, smoothing, blackborder detection, forwarder, USB/screen capture, LED device, ...), built dynamically from live data
- **HDR tone mapping select** — toggle HyperHDR's HDR tone mapping on/off
- **Priority source select** — pin the visible priority to a specific source, or set it back to auto-select
- **Adjustment numbers** — luminance gain, saturation gain, gamma, backlight threshold, and per-channel (R/G/B) temperature, one entity per field actually present on the connected instance
- **Sensors** — visible priority (with owner/RGB/component attributes), LED count, video mode, and server version
- **Clear priority button** — clears this integration's own priority on an instance
- **Instance running switch** — start/stop a HyperHDR instance (works without admin credentials on v22)
- **Dynamic multi-instance devices** — every HyperHDR instance shows up as its own Home Assistant device, created the first time it's observed running and cleanly removed (device, entities, and all) if it's later deleted from the server — all live, with no HA restart required
- **LED preview cameras** — two camera entities per instance (a to-scale LED layout preview and a soft ambient gradient preview), rendered from the live LED-color stream; **disabled by default** (enable them per-entity in the entity's settings if you want them — see [Known limitations](#known-limitations))
- **Authentication** — optional API token and optional admin password, with full reauth support when credentials go stale
- **SSDP discovery** — HyperHDR servers announcing themselves on the network are offered automatically
- **Configurable options** — default priority, hidden effects, and connection timeouts, all changeable after setup without removing the integration
- **Diagnostics** — a downloadable diagnostics payload per config entry, with tokens/passwords/host/identifiers redacted

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=metril&repository=ha-hyperhdr&category=integration)

Or manually:

1. Open HACS in your Home Assistant instance
2. Click the three-dot menu and select **Custom repositories**
3. Add `https://github.com/metril/ha-hyperhdr` with category **Integration**
4. Click **Download**
5. Restart Home Assistant

### Manual

1. Copy the `custom_components/hyperhdr` folder to your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Setup

1. Go to **Settings > Devices & Services > Add Integration**
2. Search for **HyperHDR** (or accept an auto-discovered device via SSDP)
3. Fill in the connection details:

| Field | Description | Default |
|-------|-------------|---------|
| Host | Hostname or IP of the HyperHDR server | — |
| Port | HyperHDR's JSON-RPC/WebSocket port | `8090` |
| Use SSL | Connect via `wss://` instead of `ws://` | off |
| Verify SSL | Validate the server's TLS certificate (only relevant with Use SSL on) | on |
| Token | API token, only asked for if the server requires one | — |
| Admin password | HyperHDR's admin password | — |

The token step only appears if the server reports that one is required. The admin password step is always shown but always optional — leave it blank to skip it entirely. Day-to-day control (color/effect/adjustment commands) never needs it, and starting/stopping an instance doesn't require it on the verified HyperHDR v22 either — but supplying an admin password guarantees instance start/stop keeps working on HyperHDR versions this integration hasn't been verified against, and unlocks any admin-gated feature a future release of this integration adds. If you skip it now, you can add it later via **Reconfigure** or when prompted by a reauth flow.

## Reverse proxy / Traefik

If your HyperHDR server sits behind a reverse proxy (Traefik, nginx, etc.):

- Enable **Use SSL** so the integration connects via `wss://` — plain `ws://` will not be upgraded correctly by a TLS-terminating proxy.
- **Port** is typically `443` when going through the proxy, not HyperHDR's native `8090`.
- If the proxy presents a self-signed or internal certificate, turn **Verify SSL** off.
- The proxy's route for the HyperHDR host **must pass WebSocket upgrade requests through** (`Connection: Upgrade` / `Upgrade: websocket` headers) — this integration holds the connection open for its whole lifetime, not just a single request/response.

## Entities

| Platform | Entity | Notes |
|----------|--------|-------|
| Light | *(instance device name)* | Primary light entity; RGB + brightness + effect list |
| Switch | *Component* (e.g. LED output, HDR tone mapping, Smoothing, ...) | One per reported component; CONFIG category |
| Switch | Running | Start/stop the instance; lives on the instance device but is server-scoped |
| Select | HDR tone mapping | On/off; CONFIG category |
| Select | Priority source | Auto or a specific active priority |
| Number | Luminance gain, Saturation gain, Gamma, Backlight threshold, Temperature red/green/blue | Only created for fields the connected instance actually reports; CONFIG category |
| Sensor | Visible priority | Origin/component of the currently visible priority, with `priority`/`component_id`/`rgb`/`owner` attributes |
| Sensor | LED count | DIAGNOSTIC category |
| Sensor | Video mode | DIAGNOSTIC category |
| Sensor | Version | Server-scoped, DIAGNOSTIC category |
| Button | Clear priority | Clears this integration's own priority |
| Camera | LED preview | To-scale LED layout preview; **disabled by default** |
| Camera | LED gradient | Soft, upscaled ambient preview; **disabled by default** |

Both cameras are disabled by default because rendering costs real work (either a Pillow draw + JPEG encode per still image, or a continuous LED-color stream subscription for the live MJPEG feed) — the integration never does that work unless you opt in. To enable one, go to **Settings > Devices & Services > HyperHDR**, open the instance's device, find the camera entity, and enable it from its entity settings. Neither camera is created at all for an instance whose server reports no LED layout.

## Services

All three services target a specific HyperHDR instance device via `device_id` (not the server device).

| Service | Fields | Notes |
|---------|--------|-------|
| `hyperhdr.set_color` | `device_id` (required), `rgb_color` (required), `priority` (optional), `duration` (optional, seconds) | Sets a solid color at `priority` (defaults to the integration's configured default priority) |
| `hyperhdr.set_effect` | `device_id` (required), `effect` (required), `priority` (optional), `duration` (optional, seconds) | Starts an effect by name at `priority` (defaults to the integration's configured default priority) |
| `hyperhdr.clear` | `device_id` (required), `priority` (required) | Clears the given priority; `-1` clears every priority. Not optional — a destructive action shouldn't silently default |

## Known limitations

- **No smoothing controls or average-color sensor.** HyperHDR's smoothing configuration is only reachable through an admin-gated `getconfig`/`setconfig` call pair that wasn't verified against a live server; an average-color reading needs a polled API call this push-only integration deliberately never makes. Both are out of scope for v1.
- **No `imagestream` camera.** Only the LED-color stream (`ledcolors`/`ledstream`) is used for the two camera entities. HyperHDR's separate video-preview stream (`imagestream`) is admin-gated and its frame shape was never verified live — deliberately out of scope for v1.
- **HDR mode changes made outside Home Assistant aren't picked up live.** HyperHDR doesn't push an update when HDR tone mapping is toggled from its own web UI or another client; the select entity refreshes on the next reconnect/full `serverinfo` sync, not immediately. Changes made *through* this integration's own select entity update immediately (optimistic update).

## Requirements

- A HyperHDR server reachable over the network (HyperHDR v20+; verified live against v22)
- Home Assistant 2025.1 or newer
