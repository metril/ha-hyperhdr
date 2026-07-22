"""Tests for HyperHDR data models against real captured wire fixtures."""

from __future__ import annotations

from conftest import load_fixture

from custom_components.hyperhdr.models import (
    HyperHdrAdjustment,
    HyperHdrComponent,
    HyperHdrEffect,
    HyperHdrInstanceData,
    HyperHdrInstanceSummary,
    HyperHdrPriority,
    HyperHdrServerData,
    HyperHdrSysInfo,
)

# --- HyperHdrSysInfo ---------------------------------------------------


class TestHyperHdrSysInfo:
    def test_from_dict_parses_sysinfo_fixture(self) -> None:
        raw = load_fixture("sysinfo.json")
        sysinfo = HyperHdrSysInfo.from_dict(raw["info"])
        assert sysinfo.id == "a70b962d-06b8-5d92-81f9-726384e30124"
        assert sysinfo.hostname == "a07b20766d71"
        assert sysinfo.version == "22.0.0beta2"
        assert sysinfo.build == "(HEAD detached at v22.0.0.0beta2) (Awawa-c1aaa4a/a6fa8a2-1778238667)"

    def test_from_dict_tolerates_empty_payload(self) -> None:
        sysinfo = HyperHdrSysInfo.from_dict({})
        assert sysinfo.id == ""
        assert sysinfo.hostname == ""
        assert sysinfo.version == ""
        assert sysinfo.build == ""

    def test_from_dict_tolerates_missing_nested_keys(self) -> None:
        raw = load_fixture("sysinfo.json")
        info = dict(raw["info"])
        del info["hyperhdr"]
        sysinfo = HyperHdrSysInfo.from_dict(info)
        assert sysinfo.id == ""
        assert sysinfo.hostname == "a07b20766d71"


# --- HyperHdrInstanceSummary --------------------------------------------


class TestHyperHdrInstanceSummary:
    def test_from_dict_parses_roster_entry(self) -> None:
        raw = load_fixture("serverinfo_single_instance.json")
        entry = raw["info"]["instance"][0]
        summary = HyperHdrInstanceSummary.from_dict(entry)
        assert summary.instance == 0
        assert summary.friendly_name == "First LED instance"
        assert summary.running is True

    def test_from_dict_parses_instance_update_push_entry(self) -> None:
        raw = load_fixture("instance_update_created.json")
        entries = [HyperHdrInstanceSummary.from_dict(e) for e in raw["data"]]
        assert entries[0] == HyperHdrInstanceSummary(instance=0, friendly_name="First LED instance", running=True)
        assert entries[1] == HyperHdrInstanceSummary(instance=1, friendly_name="Test Instance", running=False)

    def test_from_dict_tolerates_missing_keys(self) -> None:
        summary = HyperHdrInstanceSummary.from_dict({})
        assert summary.instance == 0
        assert summary.friendly_name == ""
        assert summary.running is False


# --- HyperHdrComponent ---------------------------------------------------


class TestHyperHdrComponent:
    def test_from_dict_parses_components_update_push(self) -> None:
        raw = load_fixture("components_update.json")
        comp = HyperHdrComponent.from_dict(raw["data"])
        assert comp.name == "SMOOTHING"
        assert comp.enabled is False

    def test_from_dict_parses_hdr_component_update(self) -> None:
        raw = load_fixture("components_update_hdr.json")
        comp = HyperHdrComponent.from_dict(raw["data"])
        assert comp.name == "HDR"
        assert comp.enabled is True

    def test_from_dict_tolerates_missing_keys(self) -> None:
        comp = HyperHdrComponent.from_dict({})
        assert comp.name == ""
        assert comp.enabled is False


# --- HyperHdrPriority -----------------------------------------------------


class TestHyperHdrPriority:
    def test_from_dict_parses_color_priority_with_rgb(self) -> None:
        raw = load_fixture("priorities_update.json")
        entry = raw["data"]["priorities"][0]
        priority = HyperHdrPriority.from_dict(entry)
        assert priority.priority == 128
        assert priority.component_id == "COLOR"
        assert priority.origin == "Fixture Capture@::ffff:172.17.0.1"
        assert priority.active is True
        assert priority.visible is True
        assert priority.rgb == (255, 0, 0)

    def test_from_dict_parses_priority_without_value(self) -> None:
        raw = load_fixture("priorities_update.json")
        entry = raw["data"]["priorities"][1]
        priority = HyperHdrPriority.from_dict(entry)
        assert priority.component_id == "VIDEOGRABBER"
        assert priority.value is None
        assert priority.rgb is None
        # owner is not present on the wire; defensive default applies.
        assert priority.owner == ""

    def test_from_dict_tolerates_missing_keys(self) -> None:
        priority = HyperHdrPriority.from_dict({})
        assert priority.priority == 0
        assert priority.component_id == ""
        assert priority.origin == ""
        assert priority.owner == ""
        assert priority.active is False
        assert priority.visible is False
        assert priority.value is None
        assert priority.rgb is None

    def test_rgb_property_none_when_value_has_no_rgb_key(self) -> None:
        priority = HyperHdrPriority.from_dict({"value": {"HSL": [0, 1, 0.5]}})
        assert priority.rgb is None


# --- HyperHdrAdjustment ----------------------------------------------------


class TestHyperHdrAdjustment:
    def test_from_dict_parses_adjustment_update_fixture(self) -> None:
        raw = load_fixture("adjustment_update.json")
        entry = raw["data"][0]
        adjustment = HyperHdrAdjustment.from_dict(entry)
        assert adjustment.luminance_gain == 0.8
        assert adjustment.saturation_gain == 1
        assert adjustment.backlight_threshold == 0.0039
        assert adjustment.gamma == 1.5
        assert adjustment.temperature_red == 1
        assert adjustment.temperature_green == 1
        assert adjustment.temperature_blue == 1

    def test_from_dict_preserves_unknown_fields_in_raw(self) -> None:
        raw = load_fixture("adjustment_update.json")
        entry = raw["data"][0]
        adjustment = HyperHdrAdjustment.from_dict(entry)
        assert adjustment.raw == entry
        assert adjustment.raw["temperatureSetting"] == "disabled"
        assert adjustment.raw["classic_config"] is True

    def test_from_dict_tolerates_empty_payload(self) -> None:
        adjustment = HyperHdrAdjustment.from_dict({})
        assert adjustment.luminance_gain is None
        assert adjustment.saturation_gain is None
        assert adjustment.backlight_threshold is None
        assert adjustment.gamma is None
        assert adjustment.temperature_red is None
        assert adjustment.temperature_green is None
        assert adjustment.temperature_blue is None
        assert adjustment.raw == {}

    def test_no_brightness_or_bare_temperature_field_exists(self) -> None:
        # v22 has no `brightness` or bare `temperature` adjustment field
        # (server rejects them outright) -- assert the model does not
        # expose such attributes so callers can't accidentally rely on them.
        adjustment = HyperHdrAdjustment.from_dict({})
        assert not hasattr(adjustment, "brightness")
        assert not hasattr(adjustment, "temperature")


# --- HyperHdrEffect ---------------------------------------------------------


class TestHyperHdrEffect:
    def test_from_dict_parses_effect_from_serverinfo(self) -> None:
        raw = load_fixture("serverinfo_single_instance.json")
        entry = raw["info"]["effects"][0]
        effect = HyperHdrEffect.from_dict(entry)
        assert effect.name == "Music: fullscreen pulse (BLUE)"
        assert effect.raw == entry

    def test_from_dict_tolerates_missing_name(self) -> None:
        effect = HyperHdrEffect.from_dict({})
        assert effect.name == ""
        assert effect.raw == {}


# --- HyperHdrServerData -----------------------------------------------------


class TestHyperHdrServerData:
    def test_instances_from_roster_single(self) -> None:
        raw = load_fixture("serverinfo_single_instance.json")
        instances = HyperHdrServerData.instances_from_roster(raw["info"]["instance"])
        assert instances == {0: HyperHdrInstanceSummary(instance=0, friendly_name="First LED instance", running=True)}

    def test_instances_from_roster_multi(self) -> None:
        raw = load_fixture("serverinfo_multi_instance.json")
        instances = HyperHdrServerData.instances_from_roster(raw["info"]["instance"])
        assert set(instances) == {0, 1}
        assert instances[1].friendly_name == "Test Instance"
        assert instances[1].running is True

    def test_instances_from_roster_reflects_push_data(self) -> None:
        raw = load_fixture("instance_update_created.json")
        instances = HyperHdrServerData.instances_from_roster(raw["data"])
        assert instances[1].running is False

    def test_instances_from_roster_empty_list(self) -> None:
        assert HyperHdrServerData.instances_from_roster([]) == {}

    def test_server_data_construction(self) -> None:
        raw = load_fixture("sysinfo.json")
        sysinfo = HyperHdrSysInfo.from_dict(raw["info"])
        server_data = HyperHdrServerData(
            sysinfo=sysinfo,
            instances=HyperHdrServerData.instances_from_roster(
                load_fixture("serverinfo_single_instance.json")["info"]["instance"]
            ),
            connected=True,
        )
        assert server_data.connected is True
        assert server_data.instances[0].running is True


# --- HyperHdrInstanceData ----------------------------------------------------


class TestHyperHdrInstanceDataFromServerinfo:
    def test_parses_components(self) -> None:
        raw = load_fixture("serverinfo_single_instance.json")
        instance_data = HyperHdrInstanceData.from_serverinfo(0, raw["info"])
        assert instance_data.instance_id == 0
        assert instance_data.components["ALL"].enabled is True
        assert instance_data.components["HDR"].enabled is False
        assert instance_data.components["SMOOTHING"].enabled is True
        assert len(instance_data.components) == 8

    def test_parses_priorities(self) -> None:
        raw = load_fixture("serverinfo_single_instance.json")
        instance_data = HyperHdrInstanceData.from_serverinfo(0, raw["info"])
        assert len(instance_data.priorities) == 1
        assert instance_data.priorities[0].component_id == "VIDEOGRABBER"
        assert instance_data.priorities_autoselect is True

    def test_parses_adjustment_element_zero(self) -> None:
        raw = load_fixture("serverinfo_single_instance.json")
        instance_data = HyperHdrInstanceData.from_serverinfo(0, raw["info"])
        assert instance_data.adjustment.luminance_gain == 1
        assert instance_data.adjustment.gamma == 1.5

    def test_parses_effects(self) -> None:
        raw = load_fixture("serverinfo_single_instance.json")
        instance_data = HyperHdrInstanceData.from_serverinfo(0, raw["info"])
        assert len(instance_data.effects) == 54
        assert instance_data.effects[0].name == "Music: fullscreen pulse (BLUE)"

    def test_led_count_is_length_of_leds_list(self) -> None:
        raw = load_fixture("serverinfo_single_instance.json")
        instance_data = HyperHdrInstanceData.from_serverinfo(0, raw["info"])
        assert instance_data.led_count == len(raw["info"]["leds"])
        assert instance_data.led_count == 1

    def test_hdr_mode_is_bare_int(self) -> None:
        raw = load_fixture("serverinfo_single_instance.json")
        instance_data = HyperHdrInstanceData.from_serverinfo(0, raw["info"])
        assert instance_data.hdr_mode == 0
        assert isinstance(instance_data.hdr_mode, int)

    def test_connected_defaults_true(self) -> None:
        raw = load_fixture("serverinfo_single_instance.json")
        instance_data = HyperHdrInstanceData.from_serverinfo(0, raw["info"])
        assert instance_data.connected is True

    def test_multi_instance_fixture_also_parses(self) -> None:
        raw = load_fixture("serverinfo_multi_instance.json")
        instance_data = HyperHdrInstanceData.from_serverinfo(1, raw["info"])
        assert instance_data.instance_id == 1
        assert len(instance_data.components) == 8

    def test_tolerates_missing_keys(self) -> None:
        instance_data = HyperHdrInstanceData.from_serverinfo(0, {})
        assert instance_data.components == {}
        assert instance_data.priorities == []
        assert instance_data.priorities_autoselect is False
        assert instance_data.effects == []
        assert instance_data.led_count == 0
        assert instance_data.hdr_mode == 0
        assert instance_data.adjustment.luminance_gain is None


class TestHyperHdrInstanceDataApplyHelpers:
    def _base(self) -> HyperHdrInstanceData:
        raw = load_fixture("serverinfo_single_instance.json")
        return HyperHdrInstanceData.from_serverinfo(0, raw["info"])

    def test_apply_components_update_returns_new_instance_and_leaves_original_unchanged(self) -> None:
        original = self._base()
        push = load_fixture("components_update.json")
        updated = original.apply_components_update(push["data"])

        assert updated is not original
        assert updated.components["SMOOTHING"].enabled is False
        # original snapshot must remain untouched (immutability).
        assert original.components["SMOOTHING"].enabled is True

    def test_apply_components_update_hdr(self) -> None:
        original = self._base()
        push = load_fixture("components_update_hdr.json")
        updated = original.apply_components_update(push["data"])
        assert updated.components["HDR"].enabled is True
        assert original.components["HDR"].enabled is False

    def test_apply_priorities_update_returns_new_instance_and_leaves_original_unchanged(self) -> None:
        original = self._base()
        push = load_fixture("priorities_update.json")
        updated = original.apply_priorities_update(push["data"])

        assert updated is not original
        assert len(updated.priorities) == 2
        assert updated.priorities[0].rgb == (255, 0, 0)
        assert updated.priorities_autoselect is True
        # original had a single VIDEOGRABBER priority entry.
        assert len(original.priorities) == 1

    def test_apply_adjustment_update_returns_new_instance_and_leaves_original_unchanged(self) -> None:
        original = self._base()
        push = load_fixture("adjustment_update.json")
        updated = original.apply_adjustment_update(push["data"])

        assert updated is not original
        assert updated.adjustment.luminance_gain == 0.8
        assert updated.adjustment.raw["temperatureSetting"] == "disabled"
        # original snapshot's adjustment (luminanceGain 1) is untouched.
        assert original.adjustment.luminance_gain == 1

    def test_apply_effects_update_returns_new_instance_and_leaves_original_unchanged(self) -> None:
        original = self._base()
        synthetic_push_data = [{"name": "Rainbow swirl fast"}, {"name": "Knight rider"}]
        updated = original.apply_effects_update(synthetic_push_data)

        assert updated is not original
        assert [e.name for e in updated.effects] == ["Rainbow swirl fast", "Knight rider"]
        # original still has the full 54-effect serverinfo snapshot.
        assert len(original.effects) == 54
