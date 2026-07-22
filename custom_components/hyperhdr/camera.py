"""Camera platform for the HyperHDR integration -- two entities per instance.

Both entities render the live LED-color stream (``ledcolors``/``ledstream``,
not the admin-gated ``imagestream`` -- see the module docstring's Constraints
note) as a still/MJPEG image, using ``serverinfo``'s ``leds[]`` geometry
(each LED's fractional ``hmin``/``hmax``/``vmin``/``vmax`` rectangle -- see
``docs/api-notes.md`` and ``models.HyperHdrLedGeometry``):

- ``led_preview``: each LED painted as its own rectangle on a 640x360
  canvas -- an accurate physical-layout preview.
- ``led_gradient``: the same frame painted onto a tiny 64x36 canvas, then
  upscaled with bilinear smoothing -- a soft ambient-lighting preview.

Both are DISABLED BY DEFAULT (``_attr_entity_registry_enabled_default``) --
rendering costs real work (a Pillow draw + JPEG encode per still, or a
continuous ledstream subscription for MJPEG), so this integration never
does that work unless a user explicitly opts in. Neither is created at all
for an instance whose ``serverinfo`` reports no LED geometry (a build/config
without an LED layout defined).

No ``imagestream`` (the video-preview stream, distinct from the LED-color
stream) camera -- it's admin-gated and its frame shape was never verified
against the live server (see docs/api-notes.md); deliberately out of scope
for v1.

Pillow (``PIL``) is imported as a core Home Assistant dependency, NOT added
to this integration's own ``manifest.json`` requirements -- HA bundles it
for every install already, and pinning/re-declaring it here would risk a
version conflict with HA's own copy.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import time
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.components.camera import Camera
from homeassistant.const import CONTENT_TYPE_MULTIPART
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from PIL import Image, ImageDraw

from .const import DEFAULT_REQUEST_TIMEOUT, OPT_REQUEST_TIMEOUT, SIGNAL_INSTANCE_READY
from .entity import HyperHdrInstanceEntity, wait_for_connected_data

if TYPE_CHECKING:
    from collections.abc import Sequence

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .client import HyperHdrInstanceClient
    from .coordinator import HyperHdrConfigEntry, HyperHdrInstanceCoordinator
    from .models import HyperHdrLedGeometry

_LOGGER = logging.getLogger(__name__)

# Output size for both cameras' still images and the MJPEG stream.
_PREVIEW_SIZE = (640, 360)
# led_gradient paints onto this tiny canvas first, then upscales with
# bilinear smoothing -- the whole point being a soft blend, not a sharp grid.
_GRADIENT_CANVAS_SIZE = (64, 36)
_JPEG_QUALITY = 80
# MJPEG stream throttle -- see handle_async_mjpeg_stream.
_MJPEG_MIN_FRAME_INTERVAL = 0.1  # ~10 fps


async def async_setup_entry(
    hass: HomeAssistant, entry: HyperHdrConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up HyperHDR camera entities."""
    runtime = entry.runtime_data
    camera_lists = await asyncio.gather(
        *(
            _entities_for_instance(entry, coordinator, instance_id)
            for instance_id, coordinator in runtime.instance_coordinators.items()
        )
    )
    entities: list[Camera] = [camera for sublist in camera_lists for camera in sublist]
    async_add_entities(entities)

    async def _add_for_instance(instance_id: int) -> None:
        coordinator = runtime.instance_coordinators[instance_id]
        async_add_entities(await _entities_for_instance(entry, coordinator, instance_id))

    entry.async_on_unload(
        async_dispatcher_connect(hass, f"{SIGNAL_INSTANCE_READY}_{entry.entry_id}", _add_for_instance)
    )


async def _entities_for_instance(
    entry: HyperHdrConfigEntry, coordinator: HyperHdrInstanceCoordinator, instance_id: int
) -> list[Camera]:
    """Build both camera entities for one instance -- or neither.

    Bounded-waits for a connected snapshot first (``wait_for_connected_data``),
    same reasoning as switch.py/number.py: the entity *set* built here is a
    one-time snapshot, and ``coordinator.data.led_geometry`` is empty on the
    disconnected placeholder. Guard: an instance whose ``serverinfo`` never
    reports LED geometry at all (no LED layout configured) gets neither
    camera, not two permanently-unavailable ones.
    """
    data = await wait_for_connected_data(coordinator)
    if not data.led_geometry:
        return []
    return [
        HyperHdrLedPreviewCamera(coordinator, entry, instance_id),
        HyperHdrLedGradientCamera(coordinator, entry, instance_id),
    ]


# --- pure render helpers (no HA/entity state -- unit-testable directly) -----------------------


def _clamp_byte(value: object) -> int:
    """Coerce ``value`` to a valid 0-255 color channel, defensively (never raises)."""
    try:
        coerced: int = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0
    return max(0, min(255, coerced))


def paint_leds(frame: Sequence[int], geometry: Sequence[HyperHdrLedGeometry], size: tuple[int, int]) -> Image.Image:
    """Paint ``frame`` (a flat ``[R,G,B,R,G,B,...]`` ledstream frame) onto a
    black ``size`` canvas, each LED filling its ``geometry`` rectangle.

    Defensive against a frame/geometry length mismatch (e.g. the LED layout
    was edited after this frame was captured) -- uses ``min(len(frame)//3,
    len(geometry))``, silently truncating/ignoring whichever side has
    extras, never raising.
    """
    width, height = size
    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    led_count = min(len(frame) // 3, len(geometry))
    if led_count == 0:
        return canvas
    draw = ImageDraw.Draw(canvas)
    for i in range(led_count):
        led = geometry[i]
        color = (_clamp_byte(frame[i * 3]), _clamp_byte(frame[i * 3 + 1]), _clamp_byte(frame[i * 3 + 2]))
        x0 = min(max(0, round(led.hmin * width)), width - 1)
        y0 = min(max(0, round(led.vmin * height)), height - 1)
        x1 = min(max(x0 + 1, round(led.hmax * width)), width)
        y1 = min(max(y0 + 1, round(led.vmax * height)), height)
        draw.rectangle([x0, y0, x1 - 1, y1 - 1], fill=color)
    return canvas


def render_led_preview(frame: Sequence[int], geometry: Sequence[HyperHdrLedGeometry]) -> Image.Image:
    """The physical LED layout preview: each LED as its own rectangle on a
    640x360 canvas."""
    return paint_leds(frame, geometry, _PREVIEW_SIZE)


def render_led_gradient(frame: Sequence[int], geometry: Sequence[HyperHdrLedGeometry]) -> Image.Image:
    """A soft ambient-lighting preview: the same frame painted onto a tiny
    64x36 canvas, then upscaled to 640x360 with bilinear smoothing."""
    small = paint_leds(frame, geometry, _GRADIENT_CANVAS_SIZE)
    return small.resize(_PREVIEW_SIZE, Image.Resampling.BILINEAR)


def aspect_fit(image: Image.Image, width: int | None, height: int | None) -> Image.Image:
    """Resize ``image`` to fit within ``width``x``height`` preserving aspect
    ratio (letterboxing is the caller's/consumer's problem, not resized in
    here). Returns ``image`` unchanged if neither bound is given."""
    if not width and not height:
        return image
    src_w, src_h = image.size
    if width and height:
        ratio = min(width / src_w, height / src_h)
    elif width:
        ratio = width / src_w
    else:
        assert height is not None  # narrowed by the leading `if not width and not height` above
        ratio = height / src_h
    if ratio == 1.0:
        return image
    new_size = (max(1, round(src_w * ratio)), max(1, round(src_h * ratio)))
    return image.resize(new_size, Image.Resampling.BILINEAR)


def encode_jpeg(image: Image.Image, quality: int = _JPEG_QUALITY) -> bytes:
    """Encode ``image`` as JPEG bytes."""
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def _extract_leds(push: dict[str, Any]) -> list[int] | None:
    """Pull the flat RGB list out of a ledstream push frame.

    Shape: ``{"command": "ledcolors-ledstream-update", "result": {"leds":
    [R,G,B,...]}, ...}`` -- note the payload key is ``result``, not
    ``data`` (unlike every other ``-update`` push topic), and it's nested
    under ``result.leds`` -- see docs/api-notes.md.
    """
    result = push.get("result")
    if not isinstance(result, dict):
        return None
    leds = result.get("leds")
    return leds if isinstance(leds, list) else None


# --- entities -----------------------------------------------------------------------------------


class _HyperHdrCameraBase(HyperHdrInstanceEntity, Camera):
    """Shared ledstream-capture/render/serve machinery for both cameras."""

    _attr_entity_registry_enabled_default = False

    def __init__(
        self, coordinator: HyperHdrInstanceCoordinator, entry: HyperHdrConfigEntry, instance_id: int, key: str
    ) -> None:
        """Initialize the camera. ``Camera.__init__`` is NOT reached via the
        normal ``super()`` chain -- ``CoordinatorEntity``'s own base
        (``BaseCoordinatorEntity``) doesn't cooperatively call further up
        the MRO -- so it's called explicitly, matching how HA's own bundled
        integrations mix ``CoordinatorEntity`` with ``Camera``."""
        super().__init__(coordinator, entry, instance_id, key)
        Camera.__init__(self)
        self._request_timeout = entry.options.get(OPT_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT)

    def _render(self, frame: list[int]) -> Image.Image:
        raise NotImplementedError

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """Start the ledstream, render ONE frame, stop it, return JPEG bytes.

        Returns ``None`` (never raises) if the instance isn't connected, or
        if no frame arrives within ``request_timeout`` -- matching every
        other camera's "no image available right now" contract.
        """
        client = self.coordinator.client
        if client is None:
            return None
        frame = await self._async_capture_one_frame(client)
        if frame is None:
            return None
        image = aspect_fit(self._render(frame), width, height)
        return encode_jpeg(image)

    async def _async_capture_one_frame(self, client: HyperHdrInstanceClient) -> list[int] | None:
        queue: asyncio.Queue[list[int]] = asyncio.Queue(maxsize=1)

        def _on_frame(push: dict[str, Any]) -> None:
            leds = _extract_leds(push)
            if leds is None:
                return
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(leds)

        await client.start_ledstream(_on_frame)
        try:
            return await asyncio.wait_for(queue.get(), timeout=self._request_timeout)
        except TimeoutError:
            return None
        finally:
            await client.stop_ledstream(_on_frame)

    async def handle_async_mjpeg_stream(self, request: web.Request) -> web.StreamResponse | None:
        """Serve a live MJPEG stream from the ledstream, throttled to ~10fps.

        Starts the ledstream ONCE for the whole stream's lifetime (refcounted
        -- see client.py), feeding a depth-1 queue that always holds only the
        latest frame (a slow consumer drops stale frames rather than queuing
        them -- docs/api-notes.md notes accumulating ledstream frames
        client-side risks a protocol-error disconnect under backpressure).
        Always stops the ledstream on the way out (``finally``), including
        on a client disconnect (``response.write`` raising) or task
        cancellation.
        """
        client = self.coordinator.client
        if client is None:
            return None

        queue: asyncio.Queue[list[int]] = asyncio.Queue(maxsize=1)

        def _on_frame(push: dict[str, Any]) -> None:
            leds = _extract_leds(push)
            if leds is None:
                return
            while not queue.empty():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(leds)

        await client.start_ledstream(_on_frame)
        try:
            response = web.StreamResponse()
            response.content_type = CONTENT_TYPE_MULTIPART.format("--frameboundary")
            await response.prepare(request)

            last_sent = 0.0
            while True:
                frame = await queue.get()
                elapsed = time.monotonic() - last_sent
                if elapsed < _MJPEG_MIN_FRAME_INTERVAL:
                    await asyncio.sleep(_MJPEG_MIN_FRAME_INTERVAL - elapsed)
                jpeg = encode_jpeg(self._render(frame))
                await response.write(
                    b"--frameboundary\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"
                )
                last_sent = time.monotonic()
        finally:
            await client.stop_ledstream(_on_frame)


class HyperHdrLedPreviewCamera(_HyperHdrCameraBase):
    """Renders the live LED layout: each LED as its own rectangle."""

    _attr_name = "LED preview"

    def __init__(self, coordinator: HyperHdrInstanceCoordinator, entry: HyperHdrConfigEntry, instance_id: int) -> None:
        """Initialize the LED preview camera."""
        super().__init__(coordinator, entry, instance_id, "led_preview")

    def _render(self, frame: list[int]) -> Image.Image:
        return render_led_preview(frame, self.coordinator.data.led_geometry)


class HyperHdrLedGradientCamera(_HyperHdrCameraBase):
    """Renders a soft, upscaled ambient-lighting preview of the LED colors."""

    _attr_name = "LED gradient"

    def __init__(self, coordinator: HyperHdrInstanceCoordinator, entry: HyperHdrConfigEntry, instance_id: int) -> None:
        """Initialize the LED gradient camera."""
        super().__init__(coordinator, entry, instance_id, "led_gradient")

    def _render(self, frame: list[int]) -> Image.Image:
        return render_led_gradient(frame, self.coordinator.data.led_geometry)
