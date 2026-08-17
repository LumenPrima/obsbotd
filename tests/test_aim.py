import math

from obsbotd.aim import (
    aim_at_pixel,
    fit_magnification,
    magnification_from_zoom_ratio,
    zoom_ratio_from_magnification,
    WIDE_HFOV_DEG,
    VERTICAL_TANGENT_CORRECTION,
)


def test_center_pixel_is_no_op():
    r = aim_at_pixel(960, 540, 1920, 1080, 1.0, 10.0, 5.0)
    assert abs(r.d_yaw) < 1e-9 and abs(r.d_pitch) < 1e-9
    assert not r.clamped and not r.over_the_top


def test_yaw_additive_at_zero_pitch():
    r = aim_at_pixel(1920, 540, 1920, 1080, 1.0, 20.0, 0.0)
    expected = -math.degrees(math.atan(math.tan(math.radians(WIDE_HFOV_DEG / 2))))
    assert abs(r.d_yaw - expected) < 1e-9
    assert abs(r.target_yaw - (20.0 + expected)) < 1e-9


def test_pitch_additive_at_center_column():
    r = aim_at_pixel(960, 1080, 1920, 1080, 1.0, 0.0, 10.0)
    tan_v = math.tan(math.radians(WIDE_HFOV_DEG / 2)) * (1080 / 1920) * VERTICAL_TANGENT_CORRECTION
    expected = math.degrees(math.atan(tan_v))
    assert abs(r.d_pitch - expected) < 1e-9


def test_right_edge_pans_right_negative_yaw():
    r = aim_at_pixel(1920, 540, 1920, 1080, 1.0, 0.0, 0.0)
    assert r.d_yaw < 0


def test_bottom_edge_pitches_down_positive():
    r = aim_at_pixel(960, 1080, 1920, 1080, 1.0, 0.0, 0.0)
    assert r.d_pitch > 0


def test_composed_rotation_differs_from_additive_when_pitched():
    r = aim_at_pixel(1800, 540, 1920, 1080, 1.0, 0.0, 30.0)
    additive = aim_at_pixel(1800, 540, 1920, 1080, 1.0, 0.0, 0.0).d_yaw
    assert abs(r.d_yaw - additive) > 0.5


def test_clamp_reported_near_yaw_limit():
    r = aim_at_pixel(0, 540, 1920, 1080, 1.0, 140.0, 0.0)
    assert r.clamped
    assert r.target_yaw == 150.0


def test_yaw_unwrap_clamps_to_near_end():
    r = aim_at_pixel(0, 540, 1920, 1080, 1.0, 145.0, 0.0)
    assert r.target_yaw == 150.0
    assert r.d_yaw > 0


def test_zoom_magnification_roundtrip():
    for ratio in (1.0, 1.25, 1.5, 2.0):
        m = magnification_from_zoom_ratio(ratio)
        assert abs(zoom_ratio_from_magnification(m) - ratio) < 1e-12
    assert magnification_from_zoom_ratio(2.0) == 4.0


def test_fit_min_axis_wins():
    m, clamped = fit_magnification(1.0, 1920, 1080, 960, 108, 0.0)
    assert abs(m - 2.0) < 1e-9 and not clamped


def test_fit_clamps_and_reports():
    m, clamped = fit_magnification(1.0, 1920, 1080, 10, 10, 0.0)
    assert m == 4.0 and clamped
