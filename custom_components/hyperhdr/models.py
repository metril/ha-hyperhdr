"""Typed data models for the HyperHDR JSON-RPC API.

Pure Python dataclasses, parsed defensively from HyperHDR wire payloads
(unknown/missing fields never raise -- see ``docs/api-notes.md`` for the
verified v22 field names this module is built against). No Home Assistant
imports live here; the HA wiring happens in a later phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Self

# --- defensive scalar coercion helpers --------------------------------------


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce ``value`` to ``int``, falling back to ``default`` on any mismatch."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_optional_int(value: Any) -> int | None:
    """Coerce ``value`` to ``int``, or ``None`` if absent/unparseable."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_optional_float(value: Any) -> float | None:
    """Coerce ``value`` to ``float``, or ``None`` if absent/unparseable."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any, default: str = "") -> str:
    """Coerce ``value`` to ``str``, falling back to ``default`` when absent."""
    if value is None:
        return default
    return str(value)


def _as_bool(value: Any, default: bool = False) -> bool:
    """Coerce ``value`` to ``bool``, falling back to ``default`` when absent."""
    if value is None:
        return default
    return bool(value)


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    """Return ``value`` as a list of dicts, dropping any non-dict entries."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


# --- sysinfo -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HyperHdrSysInfo:
    """Static server/device identity, parsed from a ``sysinfo`` response.

    ``from_dict`` expects the response's ``info`` object (i.e. the dict
    holding the ``system`` and ``hyperhdr`` sub-objects), not the whole
    top-level response.
    """

    id: str
    hostname: str
    version: str
    build: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Build from a ``sysinfo`` response's ``info`` object."""
        hyperhdr = data.get("hyperhdr")
        system = data.get("system")
        hyperhdr = hyperhdr if isinstance(hyperhdr, dict) else {}
        system = system if isinstance(system, dict) else {}
        return cls(
            id=_as_str(hyperhdr.get("id")),
            hostname=_as_str(system.get("hostName")),
            version=_as_str(hyperhdr.get("version")),
            build=_as_str(hyperhdr.get("build")),
        )


# --- instance roster -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HyperHdrInstanceSummary:
    """One entry of the instance roster (``serverinfo.info.instance[]`` or an
    ``instance-update`` push's ``data``)."""

    instance: int
    friendly_name: str
    running: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Build from a single roster entry."""
        return cls(
            instance=_as_int(data.get("instance")),
            friendly_name=_as_str(data.get("friendly_name")),
            running=_as_bool(data.get("running")),
        )


# --- components ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HyperHdrComponent:
    """A single component's enabled state (``serverinfo.info.components[]`` or
    a ``components-update`` push's ``data``)."""

    name: str
    enabled: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Build from a single component entry."""
        return cls(
            name=_as_str(data.get("name")),
            enabled=_as_bool(data.get("enabled")),
        )


# --- priorities ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HyperHdrPriority:
    """A single priority-queue entry (``serverinfo.info.priorities[]`` or a
    ``priorities-update`` push's ``data.priorities[]``)."""

    priority: int
    component_id: str
    origin: str
    owner: str
    active: bool
    visible: bool
    value: dict[str, Any] | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Build from a single priority entry."""
        value = data.get("value")
        return cls(
            priority=_as_int(data.get("priority")),
            component_id=_as_str(data.get("componentId")),
            origin=_as_str(data.get("origin")),
            owner=_as_str(data.get("owner")),
            active=_as_bool(data.get("active")),
            visible=_as_bool(data.get("visible")),
            value=value if isinstance(value, dict) else None,
        )

    @property
    def rgb(self) -> tuple[int, int, int] | None:
        """The RGB tuple for a COLOR-origin priority, or ``None``."""
        if not self.value:
            return None
        rgb = self.value.get("RGB")
        if not isinstance(rgb, list) or len(rgb) != 3:
            return None
        try:
            return (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        except (TypeError, ValueError):
            return None


# --- adjustment --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HyperHdrAdjustment:
    """A single entry of ``serverinfo.info.adjustment[]`` (v22 real field
    names -- see ``docs/api-notes.md``).

    ``brightness`` and a bare ``temperature`` field do NOT exist on
    HyperHDR v22 (the server rejects ``brightness`` outright) and are
    intentionally not modeled here.
    """

    luminance_gain: float | None
    saturation_gain: float | None
    backlight_threshold: float | None
    gamma: float | None
    temperature_red: int | None
    temperature_green: int | None
    temperature_blue: int | None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Build from a single ``adjustment`` array entry."""
        return cls(
            luminance_gain=_as_optional_float(data.get("luminanceGain")),
            saturation_gain=_as_optional_float(data.get("saturationGain")),
            backlight_threshold=_as_optional_float(data.get("backlightThreshold")),
            gamma=_as_optional_float(data.get("gamma")),
            temperature_red=_as_optional_int(data.get("temperatureRed")),
            temperature_green=_as_optional_int(data.get("temperatureGreen")),
            temperature_blue=_as_optional_int(data.get("temperatureBlue")),
            raw=dict(data),
        )


# --- effects ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HyperHdrEffect:
    """A single effect definition (``serverinfo.info.effects[]``). Kept
    minimal -- name plus the untouched payload."""

    name: str
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Build from a single effect entry."""
        return cls(name=_as_str(data.get("name")), raw=dict(data))


# --- server-scoped snapshot -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HyperHdrServerData:
    """Server-scoped snapshot: identity, instance roster, connection state."""

    sysinfo: HyperHdrSysInfo
    instances: dict[int, HyperHdrInstanceSummary]
    connected: bool

    @classmethod
    def instances_from_roster(cls, roster: list[dict[str, Any]]) -> dict[int, HyperHdrInstanceSummary]:
        """Build an ``{instance_id: summary}`` map from a roster list.

        Accepts both ``serverinfo.info.instance`` and an ``instance-update``
        push's ``data``, which share the same per-entry shape.
        """
        summaries = (HyperHdrInstanceSummary.from_dict(e) for e in _as_dict_list(roster))
        return {summary.instance: summary for summary in summaries}


# --- instance-scoped snapshot -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HyperHdrInstanceData:
    """Instance-scoped snapshot: components, priorities, adjustment, effects,
    LED/video state, connection state.

    Treated as an immutable point-in-time snapshot: every ``apply_*`` helper
    returns a *new* instance rather than mutating ``self``.
    """

    instance_id: int
    components: dict[str, HyperHdrComponent]
    priorities: list[HyperHdrPriority]
    priorities_autoselect: bool
    adjustment: HyperHdrAdjustment
    effects: list[HyperHdrEffect]
    led_count: int
    video_mode: str
    hdr_mode: int
    connected: bool

    @classmethod
    def from_serverinfo(cls, instance_id: int, info: dict[str, Any]) -> Self:
        """Build a full snapshot from a ``serverinfo`` response's ``info`` object."""
        component_entries = (HyperHdrComponent.from_dict(e) for e in _as_dict_list(info.get("components")))
        components = {c.name: c for c in component_entries}
        priorities = [HyperHdrPriority.from_dict(e) for e in _as_dict_list(info.get("priorities"))]
        effects = [HyperHdrEffect.from_dict(e) for e in _as_dict_list(info.get("effects"))]

        adjustment_entries = _as_dict_list(info.get("adjustment"))
        adjustment = HyperHdrAdjustment.from_dict(adjustment_entries[0] if adjustment_entries else {})

        leds = info.get("leds")
        led_count = len(leds) if isinstance(leds, list) else 0

        video_mode = ""
        grabbers = info.get("grabbers")
        if isinstance(grabbers, dict):
            current = grabbers.get("current")
            if isinstance(current, dict):
                video_mode = _as_str(current.get("videoMode"))

        return cls(
            instance_id=instance_id,
            components=components,
            priorities=priorities,
            priorities_autoselect=_as_bool(info.get("priorities_autoselect")),
            adjustment=adjustment,
            effects=effects,
            led_count=led_count,
            video_mode=video_mode,
            hdr_mode=_as_int(info.get("videomodehdr")),
            connected=True,
        )

    def apply_components_update(self, data: dict[str, Any]) -> Self:
        """Apply a ``components-update`` push (single ``{name, enabled}`` object)."""
        component = HyperHdrComponent.from_dict(data)
        new_components = dict(self.components)
        new_components[component.name] = component
        return replace(self, components=new_components)

    def apply_priorities_update(self, data: dict[str, Any]) -> Self:
        """Apply a ``priorities-update`` push (``data.priorities`` + ``priorities_autoselect``)."""
        priorities = [HyperHdrPriority.from_dict(e) for e in _as_dict_list(data.get("priorities"))]
        return replace(
            self,
            priorities=priorities,
            priorities_autoselect=_as_bool(data.get("priorities_autoselect"), self.priorities_autoselect),
        )

    def apply_adjustment_update(self, data: list[dict[str, Any]]) -> Self:
        """Apply an ``adjustment-update`` push (full array -- element 0 is used)."""
        entries = _as_dict_list(data)
        return replace(self, adjustment=HyperHdrAdjustment.from_dict(entries[0] if entries else {}))

    def apply_effects_update(self, data: list[dict[str, Any]]) -> Self:
        """Apply an ``effects-update`` push (full effects list)."""
        effects = [HyperHdrEffect.from_dict(e) for e in _as_dict_list(data)]
        return replace(self, effects=effects)


# --- brightness <-> luminanceGain mapping --------------------------------------------------------


def brightness_to_luminance_gain(brightness: int) -> float:
    """Map an HA brightness (0-255) to a HyperHDR ``luminanceGain`` (0.0-1.0)."""
    clamped = max(0, min(255, brightness))
    return round(clamped / 255, 3)


def luminance_gain_to_brightness(gain: float) -> int:
    """Map a HyperHDR ``luminanceGain`` (0.0-1.0) to an HA brightness (0-255)."""
    clamped = max(0.0, min(1.0, gain))
    return max(0, min(255, round(clamped * 255)))
