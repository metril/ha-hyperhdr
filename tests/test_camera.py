"""Tests for camera.py: the pure render helpers (geometry+frame -> image,
mismatch tolerance, deterministic output size) and the LED-geometry guard on
entity creation. Rendering/streaming itself (async_camera_image,
handle_async_mjpeg_stream) is exercised live, not here -- see the Phase 7+8
report's live-validation transcript.
"""

from __future__ import annotations

from typing import Any

from conftest import FakeConfigEntry, FakeHass
from PIL import Image

from custom_components.hyperhdr.camera import (
    _PREVIEW_SIZE,
    HyperHdrLedPreviewCamera,
    _entities_for_instance,
    aspect_fit,
    encode_jpeg,
    paint_leds,
    render_led_preview,
)
from custom_components.hyperhdr.coordinator import (
    HyperHdrInstanceCoordinator,
    HyperHdrRuntimeData,
    HyperHdrServerCoordinator,
)
from custom_components.hyperhdr.models import (
    HyperHdrAdjustment,
    HyperHdrInstanceData,
    HyperHdrInstanceSummary,
    HyperHdrLedGeometry,
    HyperHdrServerData,
    HyperHdrSysInfo,
)


def _row_geometry(n: int) -> list[HyperHdrLedGeometry]:
    """``n`` LEDs laid out in a single horizontal row, each ``1/n`` wide."""
    step = 1.0 / n
    return [HyperHdrLedGeometry(hmin=i * step, hmax=(i + 1) * step, vmin=0.0, vmax=1.0) for i in range(n)]


class TestPaintLeds:
    def test_paints_each_led_rectangle_with_its_color(self) -> None:
        geometry = _row_geometry(2)
        frame = [255, 0, 0, 0, 255, 0]  # LED0=red, LED1=green
        image = paint_leds(frame, geometry, (10, 10))
        assert image.size == (10, 10)
        assert image.getpixel((1, 5)) == (255, 0, 0)
        assert image.getpixel((8, 5)) == (0, 255, 0)

    def test_background_outside_led_rectangles_is_black(self) -> None:
        geometry = [HyperHdrLedGeometry(hmin=0.0, hmax=0.3, vmin=0.0, vmax=0.3)]
        frame = [255, 255, 255]
        image = paint_leds(frame, geometry, (10, 10))
        assert image.getpixel((9, 9)) == (0, 0, 0)

    def test_frame_longer_than_geometry_truncates_extras_without_raising(self) -> None:
        geometry = _row_geometry(1)  # only one LED's worth of geometry
        frame = [255, 0, 0, 0, 255, 0]  # two LEDs' worth of color data
        image = paint_leds(frame, geometry, (10, 10))
        assert image.size == (10, 10)
        assert image.getpixel((5, 5)) == (255, 0, 0)  # only LED0 is drawn

    def test_geometry_longer_than_frame_ignores_extras_without_raising(self) -> None:
        geometry = _row_geometry(3)  # three LEDs' worth of geometry
        frame = [10, 20, 30]  # only one LED's worth of color data
        image = paint_leds(frame, geometry, (10, 10))
        assert image.size == (10, 10)

    def test_empty_frame_and_geometry_returns_plain_black_canvas(self) -> None:
        image = paint_leds([], [], (10, 10))
        assert image.size == (10, 10)
        assert image.getpixel((5, 5)) == (0, 0, 0)

    def test_out_of_range_or_malformed_color_values_are_clamped_not_raised(self) -> None:
        geometry = _row_geometry(1)
        frame: list[Any] = [999, -50, "not-a-number"]
        image = paint_leds(frame, geometry, (10, 10))
        assert image.getpixel((5, 5)) == (255, 0, 0)  # 999->255, -50->0, garbage->0


class TestRenderHelpersProduceDeterministicOutputSize:
    def test_led_preview_is_full_preview_size(self) -> None:
        image = render_led_preview([1, 2, 3], _row_geometry(1))
        assert image.size == _PREVIEW_SIZE

    def test_render_helper_tolerates_a_mismatched_geometry_length_too(self) -> None:
        # LED layout edited after this frame was captured -- must render
        # defensively (truncate/ignore extras), never raise.
        frame = [1, 2, 3, 4, 5, 6, 7, 8, 9]  # 3 LEDs
        geometry = _row_geometry(1)  # only 1 LED of geometry
        assert render_led_preview(frame, geometry).size == _PREVIEW_SIZE


class TestAspectFit:
    def test_no_bounds_returns_the_image_unchanged(self) -> None:
        image = Image.new("RGB", (640, 360))
        assert aspect_fit(image, None, None).size == (640, 360)

    def test_width_only_scales_preserving_aspect_ratio(self) -> None:
        image = Image.new("RGB", (640, 360))
        assert aspect_fit(image, 320, None).size == (320, 180)

    def test_height_only_scales_preserving_aspect_ratio(self) -> None:
        image = Image.new("RGB", (640, 360))
        assert aspect_fit(image, None, 180).size == (320, 180)

    def test_both_bounds_fits_within_the_tighter_ratio(self) -> None:
        image = Image.new("RGB", (640, 360))
        result = aspect_fit(image, 100, 100)
        assert result.size[0] <= 100
        assert result.size[1] <= 100


class TestEncodeJpeg:
    def test_returns_valid_jpeg_bytes(self) -> None:
        image = Image.new("RGB", (16, 16), (128, 64, 32))
        data = encode_jpeg(image)
        assert data[:2] == b"\xff\xd8"  # JPEG SOI magic bytes
        assert data[-2:] == b"\xff\xd9"  # JPEG EOI marker
        assert len(data) > 0


def _adjustment() -> HyperHdrAdjustment:
    return HyperHdrAdjustment(
        luminance_gain=None,
        saturation_gain=None,
        backlight_threshold=None,
        gamma=None,
        temperature_red=None,
        temperature_green=None,
        temperature_blue=None,
    )


def _instance_data(**overrides: object) -> HyperHdrInstanceData:
    defaults: dict[str, object] = {
        "instance_id": 1,
        "components": {},
        "priorities": [],
        "priorities_autoselect": False,
        "adjustment": _adjustment(),
        "effects": [],
        "led_count": 0,
        "video_mode": "",
        "hdr_mode": 0,
        "connected": True,
        "led_geometry": (),
    }
    defaults.update(overrides)
    return HyperHdrInstanceData(**defaults)  # type: ignore[arg-type]


def _make_setup() -> tuple[FakeConfigEntry, HyperHdrInstanceCoordinator]:
    hass = FakeHass()
    entry = FakeConfigEntry(entry_id="entry1", unique_id="srv-uid", data={"host": "10.0.0.5", "port": 8090})
    server_coordinator = HyperHdrServerCoordinator(hass, entry, object())  # type: ignore[arg-type]
    server_coordinator.async_set_updated_data(
        HyperHdrServerData(
            sysinfo=HyperHdrSysInfo(id="dev", hostname="host", version="22", build="b"),
            instances={1: HyperHdrInstanceSummary(instance=1, friendly_name="Living Room", running=True)},
            connected=True,
        )
    )
    entry.runtime_data = HyperHdrRuntimeData(  # type: ignore[attr-defined]
        server_client=object(),  # type: ignore[arg-type]
        server_coordinator=server_coordinator,
        instance_coordinators={},
        default_priority=128,
        hidden_effects=set(),
    )
    coordinator = HyperHdrInstanceCoordinator(hass, entry, 1)
    entry.runtime_data.instance_coordinators[1] = coordinator
    return entry, coordinator


class TestEntitiesForInstanceLedGeometryGuard:
    async def test_no_led_geometry_creates_no_camera(self) -> None:
        entry, coordinator = _make_setup()
        coordinator.async_set_updated_data(_instance_data(led_geometry=()))

        entities = await _entities_for_instance(entry, coordinator, 1)

        assert entities == []

    async def test_led_geometry_present_creates_the_preview_camera_disabled_by_default(self) -> None:
        entry, coordinator = _make_setup()
        coordinator.async_set_updated_data(_instance_data(led_geometry=tuple(_row_geometry(2))))

        entities = await _entities_for_instance(entry, coordinator, 1)

        assert len(entities) == 1
        assert isinstance(entities[0], HyperHdrLedPreviewCamera)
        assert entities[0]._attr_entity_registry_enabled_default is False  # noqa: SLF001
