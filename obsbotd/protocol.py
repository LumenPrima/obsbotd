"""OBSBOT Tiny 2 vendor wire protocol.

Ported from the reverse-engineered JS codec layer (docs/spec-codec.md). Two
distinct shapes travel over the vendor Extension Unit:

- "V3" framed commands: fixed 60-byte frames with magic 0xAA, a sequence
  number, and two CRC-16/USB tokens — written to XU selector 2.
- Flat selector buffers: fixed-offset 60-byte blocks with no framing at all —
  selector 6 carries both the settings writes (FOV/HDR/face-AE/AI-mode) and
  the status snapshot read.

All multi-byte integers are little-endian.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

FRAME_LEN = 60
MAGIC = 0xAA
FLAGS_SET = 0x25
FLAGS_GET = 0x01
SENDER = 0x0A

VENDOR_SELECTOR = 0x02
SETTINGS_SELECTOR = 0x06
STATUS_SELECTOR = 0x06
STATUS_LEN = 60

CAM_SET_DEV_STATUS = (0xA0C2, 0x02)
CAM_SET_FACE_FOCUS = (0x3602, 0x02)
CAM_SET_EXPOSURE_TINY2 = (0x2982, 0x02)
CAM_GET_EXPOSURE_RANGE_TINY2 = (0x29C2, 0x02)
AI_SET_TRACK_MODE = (0x0CC4, 0x04)
UG_GET_SN = (0x18C8, 0x0D)

EXPOSURE_RAW_MIN = 1
EXPOSURE_RAW_MAX = 2500


def crc16_usb(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return (crc ^ 0xFFFF) & 0xFFFF


assert crc16_usb(b"123456789") == 0xB4C8


class FrameParseError(Exception):
    pass


@dataclass(frozen=True)
class Frame:
    seq: int
    sender: int
    receiver: int
    cmd: int
    payload: bytes


def build_frame(cmd_receiver: tuple[int, int], seq: int, payload: bytes = b"",
                flags: int = FLAGS_SET) -> bytes:
    cmd, receiver = cmd_receiver
    frame = bytearray(FRAME_LEN)
    frame[0] = MAGIC
    frame[1] = flags
    struct.pack_into("<H", frame, 2, seq & 0xFFFF)
    struct.pack_into("<H", frame, 4, 12)
    frame[8] = SENDER
    frame[9] = receiver
    struct.pack_into("<H", frame, 10, cmd)
    if payload:
        struct.pack_into("<H", frame, 12, len(payload))
        frame[16 : 16 + len(payload)] = payload
        seg = bytearray(frame[12 : 16 + len(payload)])
        seg[2:4] = b"\x00\x00"
        struct.pack_into("<H", frame, 14, crc16_usb(bytes(seg)))
    head = bytearray(frame[0:12])
    head[6:8] = b"\x00\x00"
    struct.pack_into("<H", frame, 6, crc16_usb(bytes(head)))
    return bytes(frame)


def build_get_frame(cmd_receiver: tuple[int, int], seq: int) -> bytes:
    """Header-only GET (flags 0x01) — the ONLY flavour the device answers for
    a vendor GET; the SET flags byte gets silently ignored."""
    return build_frame(cmd_receiver, seq, b"", FLAGS_GET)


def parse_frame(buf: bytes) -> Frame:
    if len(buf) < 12:
        raise FrameParseError("frame too short")
    if buf[0] != MAGIC:
        raise FrameParseError(f"bad magic 0x{buf[0]:02x}")
    head = bytearray(buf[0:12])
    head[6:8] = b"\x00\x00"
    if crc16_usb(bytes(head)) != struct.unpack_from("<H", buf, 6)[0]:
        raise FrameParseError("header CRC mismatch")
    seq = struct.unpack_from("<H", buf, 2)[0]
    cmd = struct.unpack_from("<H", buf, 10)[0]
    payload = b""
    if len(buf) >= 16:
        plen = struct.unpack_from("<H", buf, 12)[0]
        if plen > 0:
            end = 16 + plen
            if len(buf) < end:
                raise FrameParseError("truncated payload")
            seg = bytearray(buf[12:end])
            seg[2:4] = b"\x00\x00"
            if crc16_usb(bytes(seg)) != struct.unpack_from("<H", buf, 14)[0]:
                raise FrameParseError("payload CRC mismatch")
            payload = bytes(buf[16:end])
    return Frame(seq=seq, sender=buf[8], receiver=buf[9], cmd=cmd, payload=payload)


def encode_run_status(wake: bool) -> bytes:
    return bytes([0 if wake else 1, 0, 0, 0])


def encode_face_focus(enable: bool) -> bytes:
    return struct.pack("<i", 1 if enable else 0)


def encode_exposure(manual: bool, raw: int) -> bytes:
    """5 bytes: [mode] + u32le(raw). The width is load-bearing — the device
    silently discards a 4-byte payload. Mode and value must go together; the
    separate CAM_SET_EXPOSURE_MODE command is inert on this hardware."""
    return bytes([1 if manual else 0]) + struct.pack("<I", raw)


def encode_track_speed(sport: bool) -> bytes:
    return bytes([2 if sport else 0])


def settings_buffer(tag: int, value: int) -> bytes:
    buf = bytearray(FRAME_LEN)
    buf[0] = tag
    buf[1] = 0x01
    buf[2] = value & 0xFF
    return bytes(buf)


FOV_VALUE = {"wide": 0, "medium": 1, "narrow": 2}


def encode_fov(fov: str) -> bytes:
    return settings_buffer(0x04, FOV_VALUE[fov])


def encode_hdr(on: bool) -> bytes:
    return settings_buffer(0x01, 1 if on else 0)


def encode_face_ae(face: bool) -> bytes:
    return settings_buffer(0x03, 1 if face else 0)


AI_WORK_MODE = {"none": 0, "group": 1, "human": 2, "hand": 3, "whiteboard": 4, "desk": 5}
AI_FRAMING = {"normal": 0, "upper-body": 1, "close-up": 2, "headless": 3, "lower-body": 4}
AI_SCENE_MODES = ("group", "whiteboard", "desk", "hand")
AI_FRAMING_MODES = tuple(AI_FRAMING)


def encode_ai_mode(work: str, framing: str = "normal") -> bytes:
    buf = bytearray(FRAME_LEN)
    buf[0] = 0x16
    buf[1] = 0x02
    buf[2] = AI_WORK_MODE[work]
    buf[3] = AI_FRAMING[framing] if work == "human" else 0
    return bytes(buf)


AI_MODE_TABLE = {
    (0, 0): "no-tracking",
    (2, 0): "normal",
    (2, 1): "upper-body",
    (2, 2): "close-up",
    (2, 3): "headless",
    (2, 4): "lower-body",
    (5, 0): "desk",
    (4, 0): "whiteboard",
    (3, 0): "hand",
    (1, 0): "group",
}
TRACK_SPEED_TABLE = {0: "standard", 2: "sport"}
FOV_MODE_TABLE = {0: "wide", 1: "medium", 2: "narrow", 3: "custom"}


@dataclass(frozen=True)
class CameraStatus:
    awake: bool
    hdr: bool
    face_ae: bool
    ai_mode: str
    track_speed: str
    fov_mode: str
    zoom_percent: int


def decode_status(block: bytes) -> CameraStatus:
    """Flat selector-6 snapshot. NOTE: on this firmware the mid-switch AI-mode
    transient is m=6 and MUST decode to "unknown" (never "hand") so framing
    verification keeps polling."""
    if len(block) <= 0x24:
        raise FrameParseError("status block too short")
    return CameraStatus(
        awake=block[0x02] == 0,
        hdr=block[0x06] != 0,
        face_ae=block[0x07] == 1,
        ai_mode=AI_MODE_TABLE.get((block[0x18], block[0x1C]), "unknown"),
        track_speed=TRACK_SPEED_TABLE.get(block[0x24], "unknown"),
        fov_mode=FOV_MODE_TABLE.get(block[0x11], "unknown"),
        zoom_percent=block[0x04],
    )


def zoom_ratio_to_units(ratio: float, lo: int, hi: int) -> int:
    return round(lo + (hi - lo) * (ratio - 1.0) + 0.001)


def percent_to_range(pct: float, lo: int, hi: int) -> int:
    return round(lo + (hi - lo) * (pct / 100.0))


def range_to_percent(value: int, lo: int, hi: int) -> int:
    return 0 if hi == lo else round((value - lo) / (hi - lo) * 100)
