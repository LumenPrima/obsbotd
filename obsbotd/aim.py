"""Aiming geometry: pixel coordinates → gimbal angles.

Pure math, no I/O. Ported from the JS `geometry/aim.js`, whose constants were
measured against this exact camera (rotation-only homography intrinsics solve,
2026-07-25). Degrees in the public API; radians internal only.

Load-bearing hardware facts baked into the constants:
- WIDE_HFOV_DEG = 67: horizontal FOV of the 30fps capture stream at wide.
- Frame RATE selects the sensor window, not the codec: MJPEG@60 is a 1.214x
  crop of MJPEG@30. These constants describe the 30fps field — what the
  snapshot path delivers. Preview (60fps) pixels are NOT interchangeable.
- Magnification is linear in the UVC zoom ratio: m = 3*ratio - 2.
- The gimbal's yaw axis is world-vertical: yawing while pitched sweeps a cone,
  so aim composes a rotation instead of adding per-axis offsets.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

WIDE_HFOV_DEG = 67.0
VERTICAL_TANGENT_CORRECTION = 0.957
FOV_MAGNIFICATION = {"wide": 1.0, "medium": 1.15060, "narrow": 1.47073}
MIN_MAGNIFICATION = 1.0
MAX_MAGNIFICATION = 4.0
GIMBAL_YAW_LIMIT_DEG = 150.0
GIMBAL_PITCH_LIMIT_DEG = 90.0


def magnification_from_zoom_ratio(ratio: float) -> float:
    return 3.0 * ratio - 2.0


def zoom_ratio_from_magnification(m: float) -> float:
    return (m + 2.0) / 3.0


def clamp(v: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, v))


@dataclass(frozen=True)
class AimResult:
    target_yaw: float
    target_pitch: float
    d_yaw: float
    d_pitch: float
    clamped: bool
    over_the_top: bool


def _half_angle_tangents(magnification: float, frame_w: float, frame_h: float) -> tuple[float, float]:
    if not math.isfinite(magnification) or magnification < MIN_MAGNIFICATION or magnification > MAX_MAGNIFICATION:
        raise ValueError(
            f"magnification must be finite and within [1, 4], got {magnification}"
        )
    tan_h = math.tan(math.radians(WIDE_HFOV_DEG / 2)) / magnification
    tan_v = tan_h * (frame_h / frame_w) * VERTICAL_TANGENT_CORRECTION
    return tan_h, tan_v


def aim_at_pixel(
    x: float,
    y: float,
    frame_w: float,
    frame_h: float,
    magnification: float,
    current_yaw: float,
    current_pitch: float,
) -> AimResult:
    """Composed-rotation aim. Sign conventions (hardware-verified): +yaw pans
    camera-LEFT while image x grows rightward; +pitch tilts DOWN, matching
    image y. Camera coords: x right, y down, z forward."""
    tan_h, tan_v = _half_angle_tangents(magnification, frame_w, frame_h)
    u = 2.0 * x / frame_w - 1.0
    v = 2.0 * y / frame_h - 1.0
    dx = u * tan_h
    dy = v * tan_v
    dz = 1.0
    n = math.sqrt(dx * dx + dy * dy + dz * dz)
    cy = math.cos(math.radians(current_yaw))
    sy = math.sin(math.radians(current_yaw))
    cp = math.cos(math.radians(current_pitch))
    sp = math.sin(math.radians(current_pitch))
    px = dx / n
    py = (dy * cp + dz * sp) / n
    pz = (-dy * sp + dz * cp) / n
    wx = px * cy - pz * sy
    wy = py
    wz = px * sy + pz * cy
    raw_pitch = math.degrees(math.asin(clamp(wy, -1.0, 1.0)))
    raw_yaw_atan2 = math.degrees(math.atan2(-wx, wz))
    raw_yaw = current_yaw + (
        ((raw_yaw_atan2 - current_yaw + 180.0) % 360.0 + 360.0) % 360.0 - 180.0
    )
    yaw = clamp(raw_yaw, -GIMBAL_YAW_LIMIT_DEG, GIMBAL_YAW_LIMIT_DEG)
    pitch = clamp(raw_pitch, -GIMBAL_PITCH_LIMIT_DEG, GIMBAL_PITCH_LIMIT_DEG)
    hx = -sy
    hz = cy
    over_the_top = (hx * wx + hz * wz) < 0
    return AimResult(
        target_yaw=yaw,
        target_pitch=pitch,
        d_yaw=raw_yaw - current_yaw,
        d_pitch=raw_pitch - current_pitch,
        clamped=(yaw != raw_yaw or pitch != raw_pitch),
        over_the_top=over_the_top,
    )


def fit_magnification(
    magnification: float,
    frame_w: float,
    frame_h: float,
    region_w: float,
    region_h: float,
    margin: float,
) -> tuple[float, bool]:
    """Magnification that fits a region into the frame with margin. The MIN of
    the per-axis ratios is deliberate: the axis needing LESS extra zoom sets
    the target so the other axis never overflows."""
    required_raw = (
        magnification * min(frame_w / region_w, frame_h / region_h)
    ) / (1.0 + margin)
    required = clamp(required_raw, MIN_MAGNIFICATION, MAX_MAGNIFICATION)
    return required, required != required_raw


def is_16_9(frame_w: float, frame_h: float) -> bool:
    return abs(frame_w / frame_h - 16.0 / 9.0) <= 0.02
