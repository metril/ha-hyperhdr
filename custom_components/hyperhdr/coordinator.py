"""Push-style data update coordinators for the HyperHDR integration.

Two coordinators, both ``update_interval=None``: nothing ever polls. Data
only ever changes via ``async_set_updated_data`` -- fed by the client's
``on_connected``/``on_disconnected``/push callbacks (instance-scoped) or by
``__init__.py``'s setup/diff-handler flow (server-scoped).

Lifecycle model (binding, see the Phase 3 brief): coordinators persist,
clients churn. A ``HyperHdrInstanceCoordinator`` is created once, the first
time its instance is observed running, and lives until the instance is
deleted from the server -- never torn down on a mere stop. Stopping detaches
(and stops) its client, publishing a ``connected=False`` snapshot; starting
again attaches a freshly created client to the *same* coordinator, avoiding
duplicate-unique_id entity re-adds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, NamedTuple

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    ADJUSTMENT_UPDATE_TOPIC,
    COMPONENTS_UPDATE_TOPIC,
    DOMAIN,
    EFFECTS_UPDATE_TOPIC,
    PRIORITIES_UPDATE_TOPIC,
)
from .models import HyperHdrAdjustment, HyperHdrInstanceData, HyperHdrInstanceSummary, HyperHdrServerData

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .client import HyperHdrInstanceClient, HyperHdrServerClient

    # Defined here (not lazily at the bottom) since HyperHdrRuntimeData --
    # which this alias parametrizes ConfigEntry with -- is this module's.
    type HyperHdrConfigEntry = ConfigEntry[HyperHdrRuntimeData]

_LOGGER = logging.getLogger(__name__)


class InstanceDiff(NamedTuple):
    """The result of diffing two instance rosters.

    - ``created``: ids present in ``new`` but not ``old`` (regardless of running state).
    - ``started``: ids whose ``running`` flipped False->True, INCLUDING ids that are
      newly created and already running (i.e. an id can appear in both ``created``
      and ``started``).
    - ``stopped``: ids whose ``running`` flipped True->False (only for ids known in
      both old and new -- a brand-new id can never be "stopped").
    - ``removed``: ids present in ``old`` but gone from ``new``, regardless of the
      running state they had. Removal handling stops/purges unconditionally, so a
      removed id is never also reported as ``stopped``.

    All four lists are sorted ascending by instance id for deterministic output,
    independent of the input dicts' iteration order.
    """

    created: list[int]
    started: list[int]
    stopped: list[int]
    removed: list[int]


def _diff_instances(old: dict[int, HyperHdrInstanceSummary], new: dict[int, HyperHdrInstanceSummary]) -> InstanceDiff:
    """Diff two instance rosters. Pure function -- see ``InstanceDiff`` for semantics."""
    created: list[int] = []
    started: list[int] = []
    stopped: list[int] = []
    removed: list[int] = []

    for instance_id, summary in new.items():
        previous = old.get(instance_id)
        if previous is None:
            created.append(instance_id)
            if summary.running:
                started.append(instance_id)
            continue
        if summary.running and not previous.running:
            started.append(instance_id)
        elif previous.running and not summary.running:
            stopped.append(instance_id)

    for instance_id in old:
        if instance_id not in new:
            removed.append(instance_id)

    created.sort()
    started.sort()
    stopped.sort()
    removed.sort()
    return InstanceDiff(created=created, started=started, stopped=stopped, removed=removed)


def _initial_instance_data(instance_id: int) -> HyperHdrInstanceData:
    """A safe, disconnected placeholder snapshot.

    Set directly on a freshly constructed ``HyperHdrInstanceCoordinator`` so
    ``coordinator.data`` is NEVER ``None`` -- entities may be added (on
    ``SIGNAL_INSTANCE_READY``) before the instance's client has actually
    finished connecting.
    """
    return HyperHdrInstanceData(
        instance_id=instance_id,
        components={},
        priorities=[],
        priorities_autoselect=False,
        adjustment=HyperHdrAdjustment(
            luminance_gain=None,
            saturation_gain=None,
            backlight_threshold=None,
            gamma=None,
            temperature_red=None,
            temperature_green=None,
            temperature_blue=None,
        ),
        effects=[],
        led_count=0,
        video_mode="",
        hdr_mode=0,
        connected=False,
    )


class HyperHdrServerCoordinator(DataUpdateCoordinator[HyperHdrServerData]):
    """Server-scoped coordinator: sysinfo + instance roster + connection state.

    Never polls (``update_interval=None``) and never overrides
    ``_async_update_data`` -- ``__init__.py`` seeds/refreshes it exclusively
    via ``async_set_updated_data`` from the server client's connect/disconnect
    callbacks and the instance-diff handler.
    """

    def __init__(self, hass: HomeAssistant, entry: HyperHdrConfigEntry, server_client: HyperHdrServerClient) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN} server", update_interval=None, config_entry=entry)
        self.server_client = server_client


# Instance-scoped push topics with a snapshot-diff HyperHdrInstanceData.apply_*
# handler. (instance-update/videomode-update/settings-update have no per-instance
# apply_* counterpart and are intentionally left unhandled here -- see docs/api-notes.md.)
_PUSH_APPLIERS: dict[str, Callable[[HyperHdrInstanceData, Any], HyperHdrInstanceData]] = {
    COMPONENTS_UPDATE_TOPIC: HyperHdrInstanceData.apply_components_update,
    PRIORITIES_UPDATE_TOPIC: HyperHdrInstanceData.apply_priorities_update,
    ADJUSTMENT_UPDATE_TOPIC: HyperHdrInstanceData.apply_adjustment_update,
    EFFECTS_UPDATE_TOPIC: HyperHdrInstanceData.apply_effects_update,
}


class HyperHdrInstanceCoordinator(DataUpdateCoordinator[HyperHdrInstanceData]):
    """Instance-scoped coordinator: components/priorities/adjustment/effects/
    video/connection state for a single HyperHDR instance.

    Persists for the instance's whole lifetime on the server (see module
    docstring); ``attach_client``/``detach_client`` swap the underlying
    ``HyperHdrInstanceClient`` in and out across stop/start cycles.
    """

    def __init__(self, hass: HomeAssistant, entry: HyperHdrConfigEntry, instance_id: int) -> None:
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN} instance {instance_id}", update_interval=None, config_entry=entry
        )
        self.instance_id = instance_id
        self.client: HyperHdrInstanceClient | None = None
        # Seeded immediately (not via async_set_updated_data -- no listeners
        # exist yet at construction time) so .data is never None.
        self.data = _initial_instance_data(instance_id)

    def attach_client(self, client: HyperHdrInstanceClient) -> None:
        """Attach a fresh (not-yet-started) client to this coordinator.

        Wires push callbacks (topic -> apply_* -> async_set_updated_data) and
        the connect/disconnect transitions. Safe to call again later with a
        new client instance (restart after stop) -- the coordinator itself is
        untouched, only ``self.client`` and the callback wiring change.
        """
        self.client = client
        client.on_connected = self._handle_connected
        client.on_disconnected = self._handle_disconnected
        for topic, applier in _PUSH_APPLIERS.items():
            client.set_push_callback(topic, self._make_push_handler(applier))

    def detach_client(self) -> None:
        """Clear the client reference and publish a ``connected=False`` snapshot."""
        self.client = None
        if self.data is not None:
            self.async_set_updated_data(replace(self.data, connected=False))

    def _make_push_handler(
        self, applier: Callable[[HyperHdrInstanceData, Any], HyperHdrInstanceData]
    ) -> Callable[[dict[str, Any]], None]:
        def _handler(frame: dict[str, Any]) -> None:
            # Push frames are {"command": "<topic>-update", "data": <payload>};
            # apply_* wants the inner payload, not the wrapping frame.
            if self.data is None:
                return
            self.async_set_updated_data(applier(self.data, frame.get("data")))

        return _handler

    def _handle_connected(self, info: dict[str, Any]) -> None:
        self.async_set_updated_data(HyperHdrInstanceData.from_serverinfo(self.instance_id, info))

    def _handle_disconnected(self) -> None:
        if self.data is not None:
            self.async_set_updated_data(replace(self.data, connected=False))


@dataclass
class HyperHdrRuntimeData:
    """``entry.runtime_data`` payload. No AddEntitiesCallback registry here --
    platforms subscribe to the dispatcher signals in const.py instead."""

    server_client: HyperHdrServerClient
    server_coordinator: HyperHdrServerCoordinator
    instance_coordinators: dict[int, HyperHdrInstanceCoordinator]
    default_priority: int
    hidden_effects: set[str]
