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
import tempfile
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


def _record_args(device: str, duration_s: float, mic: str | None, out: str) -> list[str]:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "v4l2", "-input_format", "mjpeg", "-video_size", "1920x1080",
            "-i", device]
    if mic:
        args += ["-f", "alsa", "-i", mic]
    args += ["-t", str(duration_s), "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if mic:
        args += ["-c:a", "aac"]
    return args + ["-y", out]


def record_clip(device: str, duration_s: float, with_audio: bool = True) -> Clip:
    duration_s = min(float(duration_s), MAX_CLIP_SECONDS)
    if duration_s <= 0:
        raise CaptureError("duration_s must be positive")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = os.path.join(OUTPUT_DIR, f"obsbot-{stamp}.mp4")

    mic = find_obsbot_mic() if with_audio else None
    attempts = [mic] + ([None] if mic else [])
    last_err = ""
    for attempt_mic in attempts:
        try:
            proc = subprocess.run(
                _record_args(device, duration_s, attempt_mic, out),
                capture_output=True, timeout=duration_s + 30,
            )
        except subprocess.TimeoutExpired as e:
            raise CaptureError("ffmpeg recording did not finish in time") from e
        if proc.returncode == 0 and os.path.exists(out):
            return Clip(path=out, duration_s=duration_s,
                        size_bytes=os.path.getsize(out), audio=attempt_mic is not None)
        last_err = proc.stderr.decode(errors="replace").strip()
    raise CaptureError(f"recording failed: {last_err or 'no output produced'}")


def _display_env() -> dict[str, str] | None:
    """Environment for a GUI child. Under systemd the daemon has no display
    variables, so probe for the user's session: a Wayland socket in the
    runtime dir, else an X socket with the conventional Xauthority."""
    env = dict(os.environ)
    if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
        return env
    runtime = env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if os.path.exists(os.path.join(runtime, "wayland-0")):
        env["WAYLAND_DISPLAY"] = "wayland-0"
        env["XDG_RUNTIME_DIR"] = runtime
        return env
    if os.path.exists("/tmp/.X11-unix/X0"):
        env["DISPLAY"] = ":0"
        env.setdefault("XAUTHORITY", os.path.expanduser("~/.Xauthority"))
        return env
    return None


class Preview:
    """One ffplay window at a time. stderr goes to an unlinked temp file, not
    a pipe — a pipe nobody drains would eventually block the child."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, device: str) -> None:
        if self.running:
            raise CaptureError("a preview window is already open")
        env = _display_env()
        if env is None:
            raise CaptureError("no display session available for a preview window")
        args = ["ffplay", "-hide_banner", "-loglevel", "warning",
                "-f", "v4l2", "-input_format", "mjpeg",
                "-video_size", "1920x1080", "-framerate", "60",
                "-i", device, "-window_title", "OBSBOT preview"]
        with tempfile.TemporaryFile() as stderr_file:
            proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=stderr_file, env=env,
            )
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._proc = proc
                return
            stderr_file.seek(0)
            err = stderr_file.read().decode(errors="replace").strip()
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
