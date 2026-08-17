"""Snapshot capture via ffmpeg.

The camera sporadically emits truncated MJPEG frames (missing the EOI marker),
mostly in a burst right after stream-on. The pipeline that proved reliable in
practice (8/8 clean over repeated runs, 260-800ms per shot):

  1. grab ONE raw camera frame with ffmpeg stream-copy (no re-encode)
  2. validate it is a complete JPEG (SOI..EOI)
  3. retry a few times on truncation — each retry is a fresh device open
  4. optionally downscale/re-encode with a second ffmpeg pass

Scaling failures fall back to the validated full-size frame rather than
failing the snapshot.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

FFMPEG = "ffmpeg"
NATIVE_SIZE = "1920x1080"
MAX_ATTEMPTS = 4
GRAB_TIMEOUT_S = 10


class CaptureError(Exception):
    pass


@dataclass(frozen=True)
class Snapshot:
    jpeg: bytes
    width: int
    height: int
    attempts: int


def is_complete_jpeg(buf: bytes) -> bool:
    if len(buf) < 4 or buf[0] != 0xFF or buf[1] != 0xD8:
        return False
    end = len(buf)
    while end > 2 and buf[end - 1] == 0x00:
        end -= 1
    return buf[end - 2] == 0xFF and buf[end - 1] == 0xD9


def jpeg_dimensions(buf: bytes) -> tuple[int, int]:
    """Parse width/height from the first SOF marker; (0, 0) if unparsable."""
    i = 2
    while i + 9 < len(buf):
        if buf[i] != 0xFF:
            i += 1
            continue
        marker = buf[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = int.from_bytes(buf[i + 2 : i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(buf[i + 5 : i + 7], "big")
            width = int.from_bytes(buf[i + 7 : i + 9], "big")
            return width, height
        i += 2 + seg_len
    return 0, 0


def _grab_raw_frame(device: str) -> bytes:
    base = [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "v4l2",
            "-input_format", "mjpeg"]
    tail = ["-i", device, "-frames:v", "1", "-c:v", "copy", "-f", "mjpeg", "pipe:1"]
    for args in (base + ["-video_size", NATIVE_SIZE] + tail, base + tail):
        try:
            proc = subprocess.run(args, capture_output=True, timeout=GRAB_TIMEOUT_S)
        except subprocess.TimeoutExpired as e:
            raise CaptureError(f"ffmpeg frame grab timed out after {GRAB_TIMEOUT_S}s") from e
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
        last_error = proc.stderr.decode(errors="replace").strip()
    raise CaptureError(f"ffmpeg could not grab a frame: {last_error or 'no output'}")


def _scale_jpeg(buf: bytes, width: int, height: int, max_dim: int, quality: int) -> bytes | None:
    scale = max_dim / max(width, height)
    tw = max(2, round(width * scale / 2) * 2)
    th = max(2, round(height * scale / 2) * 2)
    q = round(31 - (quality / 100) * 29)
    q = min(31, max(2, q))
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "mjpeg", "-i", "pipe:0",
            "-vf", f"scale={tw}:{th}", "-q:v", str(q), "-f", "mjpeg", "pipe:1"]
    try:
        proc = subprocess.run(args, input=buf, capture_output=True, timeout=GRAB_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not proc.stdout or not is_complete_jpeg(proc.stdout):
        return None
    return proc.stdout


def take_snapshot(device: str, max_dim: int | None = None, quality: int = 85) -> Snapshot:
    """A failed grab (ffmpeg could not even probe one frame — seen right after
    wake, when the stream-on garbage burst is total) consumes a retry exactly
    like a truncated frame: both are the same transient, differently shaped."""
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            frame = _grab_raw_frame(device)
        except CaptureError as e:
            last_error = str(e)
            continue
        if not is_complete_jpeg(frame):
            last_error = f"truncated MJPEG frame from camera ({len(frame)} bytes)"
            continue
        width, height = jpeg_dimensions(frame)
        if max_dim and max(width, height) > max_dim:
            scaled = _scale_jpeg(frame, width, height, max_dim, quality)
            if scaled is not None:
                sw, sh = jpeg_dimensions(scaled)
                return Snapshot(jpeg=scaled, width=sw, height=sh, attempts=attempt)
        return Snapshot(jpeg=frame, width=width, height=height, attempts=attempt)
    raise CaptureError(
        f"{MAX_ATTEMPTS} consecutive frame grabs failed ({last_error}) — the camera "
        "may be mid-reset; retry, or check USB bandwidth if this persists"
    )
