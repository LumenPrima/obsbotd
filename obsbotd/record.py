"""Video clip recording and live preview via ffmpeg/ffplay.

Clips are recorded synchronously (the tool call blocks for the duration) with
a hard cap, so there is no session registry, no orphan tracking, and nothing
to leak — the trade the old server's open-ended CaptureManager existed to
manage. Preview is a single ffplay window tracked by one process handle.
"""

from __future__ import annotations

import os
import re
import glob
import subprocess
from datetime import datetime
from dataclasses import dataclass

from .capture import CaptureError

MAX_CLIP_SECONDS = 60
OUTPUT_DIR = os.path.expanduser("~/Videos/OBSBOT")


@dataclass(frozen=True)
class Clip:
    path: str
    duration_s: float
    size_bytes: int
    audio: bool


def find_obsbot_mic() -> str | None:
    """Locate the camera's built-in mic as an ALSA capture device (hw:N,0).

    Presence is not enough: /dev/snd access is granted by logind to the active
    seat user, so a user working over SSH (or with the seat parked on the
    login greeter) cannot open the device even though it exists. Only report
    a mic we can actually open — callers fall back to a silent clip."""
    for path in glob.glob("/proc/asound/card*/usbid"):
        try:
            usbid = open(path).read().strip()
        except OSError:
            continue
        if not usbid.lower().startswith("3564:"):
            continue
        m = re.search(r"card(\d+)", path)
        if not m:
            continue
        card = m.group(1)
        pcm = f"/dev/snd/pcmC{card}D0c"
        if os.access(pcm, os.R_OK | os.W_OK):
            return f"hw:{card},0"
    return None


def record_clip(device: str, duration_s: float, with_audio: bool = True) -> Clip:
    duration_s = min(float(duration_s), MAX_CLIP_SECONDS)
    if duration_s <= 0:
        raise CaptureError("duration_s must be positive")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = os.path.join(OUTPUT_DIR, f"obsbot-{stamp}.mp4")

    mic = find_obsbot_mic() if with_audio else None
    args = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "v4l2", "-input_format", "mjpeg", "-video_size", "1920x1080",
            "-i", device]
    if mic:
        args += ["-f", "alsa", "-i", mic]
    args += ["-t", str(duration_s), "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if mic:
        args += ["-c:a", "aac"]
    args += ["-y", out]

    try:
        proc = subprocess.run(args, capture_output=True, timeout=duration_s + 30)
    except subprocess.TimeoutExpired as e:
        raise CaptureError("ffmpeg recording did not finish in time") from e
    if proc.returncode != 0 or not os.path.exists(out):
        err = proc.stderr.decode(errors="replace").strip()
        raise CaptureError(f"recording failed: {err or 'no output produced'}")
    return Clip(path=out, duration_s=duration_s,
                size_bytes=os.path.getsize(out), audio=mic is not None)


class Preview:
    """One ffplay window at a time. Needs a display session; the daemon unit
    passes DISPLAY/WAYLAND_DISPLAY through so this works under systemd too."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, device: str) -> None:
        if self.running:
            raise CaptureError("a preview window is already open")
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise CaptureError("no display session available for a preview window")
        args = ["ffplay", "-hide_banner", "-loglevel", "warning",
                "-f", "v4l2", "-input_format", "mjpeg",
                "-video_size", "1920x1080", "-framerate", "60",
                "-i", device, "-window_title", "OBSBOT preview"]
        proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            self._proc = proc
            return
        err = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace").strip()
        raise CaptureError(
            f"preview window failed to open ({err.splitlines()[-1] if err else 'ffplay exited immediately'}) "
            "— is a desktop session active for this user?"
        )

    def stop(self) -> bool:
        if not self.running:
            self._proc = None
            return False
        assert self._proc is not None
        self._proc.terminate()
        try:
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None
        return True
