"""Translation keys used by platforms exist in strings.json; en.json mirrors it."""

from __future__ import annotations

import json
import re
from pathlib import Path

from custom_components.hyperhdr.const import COMPONENT_LABELS

_DIR = Path(__file__).parent.parent / "custom_components" / "hyperhdr"
_KEY_RE = re.compile(r'_attr_translation_key = "([a-z_]+)"')


def _load(rel: str) -> dict:
    return json.loads((_DIR / rel).read_text())


def test_strings_equal_en() -> None:
    assert _load("strings.json") == _load("translations/en.json")


def test_every_platform_translation_key_exists() -> None:
    entity = _load("strings.json")["entity"]
    for platform in ("button", "camera", "select", "switch", "sensor", "number", "light"):
        keys = set(_KEY_RE.findall((_DIR / f"{platform}.py").read_text()))
        for key in keys:
            assert key in entity.get(platform, {}), f"{platform}.{key} missing"
        assert all("name" in v for v in entity.get(platform, {}).values())


def test_component_labels_have_switch_translations() -> None:
    switches = _load("strings.json")["entity"]["switch"]
    for component, label in COMPONENT_LABELS.items():
        assert switches[component.lower()]["name"] == label
