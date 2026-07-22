"""Tests for the HA brightness <-> HyperHDR luminanceGain mapping helpers."""

from __future__ import annotations

import pytest

from custom_components.hyperhdr.models import (
    brightness_to_luminance_gain,
    luminance_gain_to_brightness,
)


@pytest.mark.parametrize(
    ("brightness", "expected"),
    [
        (0, 0.0),
        (255, 1.0),
        (127, 0.498),
        (254, 0.996),
        (1, 0.004),
    ],
)
def test_brightness_to_luminance_gain_boundaries(brightness: int, expected: float) -> None:
    assert brightness_to_luminance_gain(brightness) == expected


def test_brightness_to_luminance_gain_clamps_below_zero() -> None:
    assert brightness_to_luminance_gain(-10) == 0.0


def test_brightness_to_luminance_gain_clamps_above_255() -> None:
    assert brightness_to_luminance_gain(300) == 1.0


@pytest.mark.parametrize(
    ("gain", "expected"),
    [
        (0.0, 0),
        (0.5, 128),
        (1.0, 255),
    ],
)
def test_luminance_gain_to_brightness_values(gain: float, expected: int) -> None:
    assert luminance_gain_to_brightness(gain) == expected


def test_luminance_gain_to_brightness_clamps_below_zero() -> None:
    assert luminance_gain_to_brightness(-0.5) == 0


def test_luminance_gain_to_brightness_clamps_above_one() -> None:
    assert luminance_gain_to_brightness(1.5) == 255


@pytest.mark.parametrize("brightness", [0, 1, 2, 63, 127, 128, 200, 253, 254, 255])
def test_round_trip_within_one_step(brightness: int) -> None:
    gain = brightness_to_luminance_gain(brightness)
    round_tripped = luminance_gain_to_brightness(gain)
    assert abs(round_tripped - brightness) <= 1
