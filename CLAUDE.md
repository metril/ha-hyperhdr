# ha-hyperhdr

Home Assistant custom integration for [HyperHDR](https://github.com/awawa-dev/HyperHDR) ambient lighting.
**Version:** 0.1.0 | **Domain:** `hyperhdr` | **IoT Class:** `local_push`

## Project Structure

```
custom_components/hyperhdr/
├── __init__.py        (416)  Entry setup/teardown, dynamic instance lifecycle diffing, session wiring, service (un)registration
├── client.py           (773)  ALL WebSocket/JSON-RPC transport (only file importing aiohttp for the wire protocol) -- connect/auth/reconnect/watchdog + server/instance clients + ledstream fan-out
├── const.py             (123)  Domain, config/option keys, defaults, subscription topics, component labels, dispatcher signal names
├── coordinator.py       (243)  HyperHdrServerCoordinator + HyperHdrInstanceCoordinator (push-only, update_interval=None), instance roster diffing
├── entity.py             (195)  Server-scoped/instance-scoped entity bases, device_info builders, wait_for_connected_data
├── exceptions.py          (32)  HyperHdrError hierarchy (Connection/Api/Auth)
├── flow_support.py       (278)  Pure config-flow support: connection validation, SSDP location parsing, voluptuous schema builders (no HA flow-manager imports -- unit testable directly)
├── config_flow.py        (340)  ConfigFlow/OptionsFlow orchestration: user/ssdp/auth/admin/reauth/reconfigure/options steps
├── models.py             (389)  Typed dataclasses parsed defensively from HyperHDR wire payloads
├── diagnostics.py         (81)  Config entry diagnostics with secret/identity redaction
├── services.py           (185)  Device-targeted set_color/set_effect/clear service registration + handlers
├── services.yaml           (68)  HA service field/selector definitions (mirrors services.py's schemas)
├── light.py              (162)  Primary per-instance RGB light entity
├── switch.py              (179)  Component switches (dynamic) + instance running switch
├── select.py              (144)  HDR tone mapping select + priority source select
├── number.py              (211)  Presence-based adjustment-field number entities
├── sensor.py              (177)  Visible priority / LED count / video mode / server version sensors
├── button.py               (63)  Clear-priority button, one per instance
├── camera.py              (340)  LED preview + gradient camera entities (disabled by default), MJPEG stream + still capture
├── manifest.json            (17)  Integration metadata
├── strings.json            (165)  Translation source (config flow strings)
├── translations/en.json    (165)  English translations (mirrors strings.json)
└── brand/                        HACS brand assets: icon.png, icon@2x.png, logo.png, logo@2x.png

tests/
├── conftest.py           (1072)  Scripted fake aiohttp WebSocket/session, HA entity-platform stubs, fixture loaders, a command-recording fake client
├── fixtures/                     Real captured HyperHDR wire payloads (serverinfo, push updates, error responses, ...)
├── test_client_framing.py (598)  Tan correlation, push routing (incl. ledstream fan-out), malformed-frame handling, instance command payloads, ledstream/imagestream refcounting
├── test_client_reconnect.py (400)  Reconnect supervisor (backoff/jitter), staleness watchdog, synthetic instance-lifecycle roster pushes
├── test_client_auth.py    (259)  Connect-time auth handshake: tokenRequired/login/admin
├── test_coordinator_instances.py (419)  Instance-lifecycle diff logic and its orchestration
├── test_entry_setup.py    (368)  async_setup_entry/async_unload_entry: session selection, first-connect wait, reauth-during-setup
├── test_models.py         (363)  HyperHDR data models against real captured wire fixtures
├── test_flow_support.py   (237)  Pure config-flow support: validation outcomes, SSDP parsing, schema shapes
├── test_light.py          (323)  State mapping and turn_on/turn_off command selection
├── test_switch.py         (246)  Dynamic component-switch build from data.components, running switch
├── test_select.py         (211)  HDR mode mapping and priority_source option building/selection
├── test_number.py         (208)  Presence-based entity creation and single-field adjustment writes
├── test_services.py       (218)  Device-target resolution helper and the three service handlers
├── test_camera.py         (207)  Pure render helpers (geometry+frame -> image) and the LED-geometry entity-creation guard
├── test_diagnostics.py    (185)  Redaction coverage and diagnostics payload shape
├── test_entity_bases.py   (155)  Server/instance base entity classes
├── test_sensor.py         (139)  value_fn/attrs_fn evaluation for each sensor description
├── test_button.py          (70)  Press behavior and error wrapping
└── test_brightness_mapping.py (59)  HA brightness <-> HyperHDR luminanceGain mapping helpers
```

- `docs/` — API recon notes and phase reports (local only, gitignored)

## Development

```bash
uv sync --all-extras
uv run pytest                       # 300+ tests, no warnings
uv run ruff check .
uv run ruff format .
uv run mypy
```

Live/dev HyperHDR server (used for the recon and live-validation this integration was built against, not part of CI):

```bash
docker run -d --name hyperhdr-dev -p 8090:8090 -p 19444:19444 -p 19400:19400 gpregger/hyperhdr:latest
```

A scratchpad Home Assistant dev environment (venv + config) was used for live validation against that container. It is disposable local tooling, not part of this repo -- there's nothing under version control to set up or maintain for it.

## Architecture

```
HyperHdrServerClient (1 socket: sysinfo, instance roster, start/stop/create/delete instance)
HyperHdrInstanceClient x N (1 socket per RUNNING instance: switchTo, color/effect/adjustment/component/HDR/source commands)
        │ push (topic -> apply_* / connected snapshot)
        ▼
HyperHdrServerCoordinator          HyperHdrInstanceCoordinator x N
        │                                   │
        └──────────────┬────────────────────┘
                        ▼
      light / switch / select / number / sensor / button / camera platforms
```

- **N+1 sockets per config entry**: one persistent `HyperHdrServerClient` connection plus one `HyperHdrInstanceClient` connection per currently-*running* instance -- a stopped instance has no open socket at all.
- **Push flow**: both client classes subscribe (via `serverinfo`'s `subscribe` list) to `*-update` topics on connect; `client.py`'s `_route_message` routes anything ending `-update` to a registered push callback (never resolves a pending request future -- see `client.py`'s tan-correlation note). `coordinator.py` wires one `apply_*` handler per topic, each producing a fresh immutable `HyperHdrInstanceData`/`HyperHdrServerData` snapshot via `dataclasses.replace` and publishing it with `async_set_updated_data`.
- **Instance lifecycle signals**: `__init__.py`'s `_async_handle_instance_diff` diffs the roster on every reconnect/roster push and fires four dispatcher signals per instance id as appropriate -- `created` (device/entities not yet built), `started`/`stopped` (client attach/detach on a persistent coordinator), `removed` (full purge). `SIGNAL_INSTANCE_ADDED` lets `switch.py`'s running switch exist before any `HyperHdrInstanceCoordinator` does; `SIGNAL_INSTANCE_READY` fires once, at coordinator creation, and is what every other data-driven platform listens for to add its entities.

## Key Conventions

- **Tan-only response correlation.** HyperHDR blanks or rewrites `command` on errors and subcommand acks; every request/response match is by `tan` alone, never by command name (see `client.py`'s module docstring and `docs/api-notes.md`).
- **Field-scoped adjustment writes.** `async_set_adjustment(**fields)` sends only the fields actually passed, camelCase-mapped -- never the full cached adjustment object echoed back (that class of bug silently reverts every field you didn't mean to touch).
- **Presence-based entity creation.** `number.py`'s adjustment fields and `switch.py`'s component switches are built only for what a *connected* snapshot actually reports (`wait_for_connected_data`, bounded-wait) -- never a fixed list, so this integration never claims to control something a given HyperHDR build/config doesn't expose.
- **Coordinator persists, client churns.** A `HyperHdrInstanceCoordinator` is created once (first observed running) and lives until its instance is deleted from the server roster -- stopping an instance detaches+stops its client and publishes `connected=False`, but never tears down the coordinator or its entities. Starting again attaches a freshly created client to the *same* coordinator (avoids duplicate-unique_id entity re-adds).
- **Unique ID formats:** server-scoped `{server_uid}_{key}`; instance-scoped `{server_uid}_{instance_id}_{key}`. `server_uid` is the config entry's `unique_id` (the server's own sysinfo `id`), falling back to `entry_id` for entries/tests that predate one.
- **`from __future__ import annotations`** at the top of every module, project-wide.
- **No admin-gating assumptions baked in as blanket rules.** Whether a given HyperHDR command needs an admin login varies by command (and possibly by version) and is verified live per-command, not assumed -- see the docstrings on `client.py`'s instance-lifecycle/stream methods.
- **`client.py`'s `create_instance`/`delete_instance` are reserved for future use.** Transport-layer support exists (and is tested) but nothing HA-facing (service, config flow, UI action) calls them yet -- intentional, not dead code to prune.

## Rules

- **Never commit `docs/`** -- API recon notes and phase reports are local working files (gitignored).
- **Never mention AI/Claude** in commits or code.
- **Git identity:** author `metril <1517921+metril@users.noreply.github.com>`.
