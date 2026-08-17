"""High-level camera operations.

Every operation opens the device fresh (see discovery.py) — the daemon holds
no fd between calls, which is what makes plug/unplug handling automatic. The
vendor sequence counter is the only cross-call state (1..0xFFFF, never 0).

Sign conventions (hardware-verified): +yaw pans camera-LEFT, +pitch tilts
DOWN. V4L2 pan shares yaw's sign; V4L2 tilt is +up, so pitch is negated on
both write and read. Units: 3600 arc-seconds per degree.

Honesty note: uvcvideo caches CT_PANTILT_ABSOLUTE (non-volatile on this
kernel), so gimbal readback is the LAST-COMMANDED pose, not a live one.
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, asdict

from . import protocol as proto
from .discovery import CameraInfo, find_camera
from .uvc import (
    VideoDevice,
    CID_PAN_ABSOLUTE,
    CID_TILT_ABSOLUTE,
    CID_ZOOM_ABSOLUTE,
    CID_FOCUS_AUTO,
    CID_FOCUS_ABSOLUTE,
    CID_BRIGHTNESS,
    CID_CONTRAST,
    CID_SATURATION,
    CID_HUE,
    CID_SHARPNESS,
    CID_AUTO_WHITE_BALANCE,
    CID_WHITE_BALANCE_TEMPERATURE,
)

ASEC_PER_DEG = 3600
YAW_LIMIT_DEG = 150.0
PITCH_LIMIT_DEG = 90.0
WAKE_POLL_S = 0.2
WAKE_TIMEOUT_S = 2.5
WAKE_SETTLE_S = 0.3
SNAPSHOT_WAKE_SETTLE_S = 1.5
ZOOM_SETTLE_POLL_S = 0.1
ZOOM_SETTLE_TIMEOUT_S = 3.0
ZOOM_SETTLE_TOLERANCE_PCT = 1
FRAMING_VERIFY_ATTEMPTS = 30
FRAMING_VERIFY_INTERVAL_S = 0.2

IMAGE_CONTROL_CIDS = {
    "brightness": CID_BRIGHTNESS,
    "contrast": CID_CONTRAST,
    "saturation": CID_SATURATION,
    "hue": CID_HUE,
    "sharpness": CID_SHARPNESS,
}

_seq_lock = threading.Lock()
_seq = 0


def next_seq() -> int:
    global _seq
    with _seq_lock:
        _seq = 1 if _seq >= 0xFFFF else _seq + 1
        return _seq


@dataclass(frozen=True)
class GimbalPose:
    yaw: float
    pitch: float


class Camera:
    """One discovered camera; methods open/close the device per operation."""

    def __init__(self, info: CameraInfo):
        self.info = info

    @classmethod
    def find(cls) -> "Camera":
        return cls(find_camera())

    def _dev(self) -> VideoDevice:
        return VideoDevice(self.info.path)

    def send_vendor(self, cmd_receiver: tuple[int, int], payload: bytes = b"") -> None:
        frame = proto.build_frame(cmd_receiver, next_seq(), payload)
        with self._dev() as dev:
            dev.xu_set(self.info.xu_unit, proto.VENDOR_SELECTOR, frame)

    def write_settings(self, buf: bytes) -> None:
        with self._dev() as dev:
            dev.xu_set(self.info.xu_unit, proto.SETTINGS_SELECTOR, buf)

    def status(self) -> proto.CameraStatus:
        with self._dev() as dev:
            block = dev.xu_get(self.info.xu_unit, proto.STATUS_SELECTOR, proto.STATUS_LEN)
        return proto.decode_status(block)

    def wake(self) -> None:
        self.send_vendor(proto.CAM_SET_DEV_STATUS, proto.encode_run_status(True))

    def sleep(self) -> None:
        self.send_vendor(proto.CAM_SET_DEV_STATUS, proto.encode_run_status(False))

    def ensure_awake(self, settle_s: float = WAKE_SETTLE_S) -> bool:
        """Wake the camera if asleep. Returns True if a wake was needed —
        waking un-stows the gimbal, so any frame measured before it is stale."""
        try:
            if self.status().awake:
                return False
        except OSError:
            return False
        self.wake()
        deadline = time.monotonic() + WAKE_TIMEOUT_S
        while time.monotonic() < deadline:
            time.sleep(WAKE_POLL_S)
            try:
                if self.status().awake:
                    break
            except OSError:
                continue
        time.sleep(settle_s)
        return True

    def gimbal_set(self, yaw_deg: float, pitch_deg: float) -> GimbalPose:
        """Both axes in ONE ioctl (see uvc.set_controls for why that is
        load-bearing). Clamps to the mechanical range."""
        yaw = max(-YAW_LIMIT_DEG, min(YAW_LIMIT_DEG, yaw_deg))
        pitch = max(-PITCH_LIMIT_DEG, min(PITCH_LIMIT_DEG, pitch_deg))
        with self._dev() as dev:
            dev.set_controls([
                (CID_PAN_ABSOLUTE, round(yaw * ASEC_PER_DEG)),
                (CID_TILT_ABSOLUTE, round(-pitch * ASEC_PER_DEG)),
            ])
        return GimbalPose(yaw=yaw, pitch=pitch)

    def gimbal_recenter(self) -> None:
        self.gimbal_set(0.0, 0.0)

    def gimbal_get(self) -> GimbalPose:
        with self._dev() as dev:
            pan, tilt = dev.get_controls([CID_PAN_ABSOLUTE, CID_TILT_ABSOLUTE])
        return GimbalPose(yaw=pan / ASEC_PER_DEG, pitch=-tilt / ASEC_PER_DEG)

    def zoom_set(self, ratio: float) -> tuple[float, bool]:
        ratio = max(1.0, min(2.0, ratio))
        with self._dev() as dev:
            rng = dev.query_control_range(CID_ZOOM_ABSOLUTE)
            dev.set_controls([
                (CID_ZOOM_ABSOLUTE, proto.zoom_ratio_to_units(ratio, rng.minimum, rng.maximum))
            ])
        target_pct = (ratio - 1.0) * 100.0
        deadline = time.monotonic() + ZOOM_SETTLE_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                if abs(self.status().zoom_percent - target_pct) <= ZOOM_SETTLE_TOLERANCE_PCT:
                    return ratio, True
            except (OSError, proto.FrameParseError):
                pass
            time.sleep(ZOOM_SETTLE_POLL_S)
        return ratio, False

    def read_ai_mode(self) -> str:
        try:
            return self.status().ai_mode
        except (OSError, proto.FrameParseError):
            return "unknown"

    def ai_track(self, enabled: bool, mode: str) -> tuple[str, bool]:
        """Write the AI mode, then poll until the camera lands somewhere
        stable. The m=6 'unknown' transient parks for 3-4s mid-switch, hence
        the 6s verification ceiling."""
        before = self.read_ai_mode()
        if not enabled:
            payload = proto.encode_ai_mode("none")
            want = "no-tracking"
        elif mode in proto.AI_SCENE_MODES:
            payload = proto.encode_ai_mode(mode)
            want = mode
        else:
            payload = proto.encode_ai_mode("human", mode)
            want = mode
        self.write_settings(payload)
        verified = before
        for attempt in range(FRAMING_VERIFY_ATTEMPTS):
            if attempt:
                time.sleep(FRAMING_VERIFY_INTERVAL_S)
            verified = self.read_ai_mode()
            if verified == want:
                return verified, True
            if verified != "unknown" and verified != before:
                return verified, False
        return verified, False

    def focus_auto(self) -> None:
        with self._dev() as dev:
            dev.set_controls([(CID_FOCUS_AUTO, 1)])

    def focus_manual(self, position_pct: float) -> int:
        with self._dev() as dev:
            dev.set_controls([(CID_FOCUS_AUTO, 0)])
            rng = dev.query_control_range(CID_FOCUS_ABSOLUTE)
            value = proto.percent_to_range(position_pct, rng.minimum, rng.maximum)
            dev.set_controls([(CID_FOCUS_ABSOLUTE, value)])
        return value

    def face_focus(self, enable: bool) -> None:
        self.send_vendor(proto.CAM_SET_FACE_FOCUS, proto.encode_face_focus(enable))

    def read_focus(self) -> dict[str, object]:
        try:
            with self._dev() as dev:
                auto = dev.get_controls([CID_FOCUS_AUTO])[0]
                if auto:
                    return {"focus_mode": "auto"}
                value = dev.get_controls([CID_FOCUS_ABSOLUTE])[0]
                rng = dev.query_control_range(CID_FOCUS_ABSOLUTE)
            return {
                "focus_mode": "manual",
                "focus_position": proto.range_to_percent(value, rng.minimum, rng.maximum),
            }
        except OSError:
            return {"focus_mode": "unknown"}

    def exposure(self, manual: bool, level_pct: float = 50.0) -> int:
        raw = proto.percent_to_range(
            level_pct, proto.EXPOSURE_RAW_MIN, proto.EXPOSURE_RAW_MAX
        )
        self.send_vendor(proto.CAM_SET_EXPOSURE_TINY2, proto.encode_exposure(manual, raw))
        return raw

    def face_ae(self, face: bool) -> None:
        self.write_settings(proto.encode_face_ae(face))

    def hdr(self, on: bool) -> None:
        self.write_settings(proto.encode_hdr(on))

    def fov(self, mode: str) -> None:
        self.write_settings(proto.encode_fov(mode))

    def track_speed(self, sport: bool) -> None:
        self.send_vendor(proto.AI_SET_TRACK_MODE, proto.encode_track_speed(sport))

    def image_adjust(self, control: str, level_pct: float) -> int:
        cid = IMAGE_CONTROL_CIDS[control]
        with self._dev() as dev:
            rng = dev.query_control_range(cid)
            value = proto.percent_to_range(level_pct, rng.minimum, rng.maximum)
            dev.set_controls([(cid, value)])
        return value

    def white_balance(self, temperature_k: int | None) -> int | None:
        """None = auto; a Kelvin value switches to manual at that temperature."""
        with self._dev() as dev:
            if temperature_k is None:
                dev.set_controls([(CID_AUTO_WHITE_BALANCE, 1)])
                return None
            rng = dev.query_control_range(CID_WHITE_BALANCE_TEMPERATURE)
            value = max(rng.minimum, min(rng.maximum, round(temperature_k)))
            dev.set_controls([(CID_AUTO_WHITE_BALANCE, 0)])
            dev.set_controls([(CID_WHITE_BALANCE_TEMPERATURE, value)])
        return value

    def status_dict(self) -> dict[str, object]:
        st = self.status()
        pose = self.gimbal_get()
        return {
            **asdict(st),
            **self.read_focus(),
            "gimbal_last_commanded": {"yaw": round(pose.yaw, 2), "pitch": round(pose.pitch, 2)},
            "device": self.info.path,
            "card": self.info.card,
        }
