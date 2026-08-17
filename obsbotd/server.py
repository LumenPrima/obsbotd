"""obsbotd — OBSBOT camera MCP daemon.

One daemon owns the camera and serves every MCP client over streamable-http
(127.0.0.1:8626/mcp). There is no ownership election and no helper process:
tool calls discover the device fresh (hotplug-proof), talk V4L2/UVC directly,
and capture through ffmpeg.

Error contract: an unplugged/absent camera is a normal, structured result —
{"ok": false, "camera_present": false, ...} — never a protocol error, so
agents can react ("the camera is currently unplugged") instead of crashing.
"""

from __future__ import annotations

import os
import json
import errno
import atexit
import asyncio
import time
import traceback
from typing import Literal

from mcp.server.mcpserver import MCPServer, Image

from . import protocol as proto
from .aim import (
    aim_at_pixel,
    fit_magnification,
    is_16_9,
    magnification_from_zoom_ratio,
    zoom_ratio_from_magnification,
    FOV_MAGNIFICATION,
    MIN_MAGNIFICATION,
    MAX_MAGNIFICATION,
)
from .camera import Camera, SNAPSHOT_WAKE_SETTLE_S
from .capture import CaptureError, take_snapshot
from .discovery import CameraUnavailable
from .record import MAX_CLIP_SECONDS, Preview, record_clip

HOST = os.environ.get("OBSBOTD_HOST", "127.0.0.1")
PORT = int(os.environ.get("OBSBOTD_PORT", "8626"))
STEADY_ZOOM_POLL_S = 0.08

mcp = MCPServer(
    name="obsbot",
    instructions=(
        "OBSBOT Tiny 2 Lite camera control. The camera is FAST: snapshots need no "
        "settle delay unless the gimbal just moved (then settle_ms=1200 suffices). "
        "Snapshots auto-wake a sleeping camera. Gimbal control is open-loop on "
        "Linux: obsbot_gimbal_position reports the last-commanded pose, not a "
        "live reading — verify pointing visually with a snapshot. If a tool "
        "reports camera_present=false, the camera is not available right now "
        "(unplugged or re-enumerating); report that — the next call after a "
        "replug just works."
    ),
)

_lock = asyncio.Lock()
_preview = Preview()
atexit.register(_preview.stop)


def _unavailable(e: Exception) -> dict[str, object]:
    return {"ok": False, "camera_present": False, "error": str(e)}


def _disconnected(e: OSError) -> dict[str, object]:
    return {
        "ok": False,
        "camera_present": False,
        "error": f"the camera stopped responding mid-operation ({e}) — it may have been unplugged; retry when reconnected",
    }


_CAMERA_LOSS_ERRNOS = {errno.ENODEV, errno.ENXIO, errno.EIO}


def _is_camera_loss(e: OSError) -> bool:
    """Only device-gone signals map to the unplugged contract. Other OSErrors
    (missing ffmpeg binary, permission problems, filesystem failures) must
    NOT masquerade as an unplugged camera."""
    if e.errno in _CAMERA_LOSS_ERRNOS:
        return True
    filename = getattr(e, "filename", None)
    return isinstance(filename, str) and filename.startswith("/dev/video")


async def _run(fn, *args):
    """Run a blocking camera op off the event loop, holding the camera lock.
    Every failure becomes a structured {ok: false, ...} result — a raw
    exception must never reach the MCP layer as an opaque tool error."""
    async with _lock:
        try:
            return await asyncio.to_thread(fn, *args)
        except CameraUnavailable as e:
            return _unavailable(e)
        except (CaptureError, proto.FrameParseError) as e:
            return {"ok": False, "error": str(e)}
        except OSError as e:
            if _is_camera_loss(e):
                return _disconnected(e)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"internal error: {type(e).__name__}: {e}"}


def _steady_status(cam: Camera) -> tuple[proto.CameraStatus | None, dict[str, object] | None]:
    first = cam.status()
    time.sleep(STEADY_ZOOM_POLL_S)
    second = cam.status()
    if first.zoom_percent != second.zoom_percent:
        return None, {
            "ok": False,
            "error": (
                f"the zoom is still moving (zoomPercent read {first.zoom_percent} then "
                f"{second.zoom_percent}), so the frame you measured no longer matches the "
                "current magnification. Wait for it to finish, take a fresh snapshot, and retry."
            ),
        }
    return second, None


def _resolve_magnification(st: proto.CameraStatus) -> tuple[float | None, dict[str, object] | None]:
    if st.fov_mode == "unknown":
        return None, {
            "ok": False,
            "error": "the FOV mode byte did not decode — set a known mode with obsbot_image_mode (fov) and retry",
        }
    if st.fov_mode == "custom":
        m = magnification_from_zoom_ratio(1.0 + st.zoom_percent / 100.0)
        if not (MIN_MAGNIFICATION <= m <= MAX_MAGNIFICATION):
            return None, {
                "ok": False,
                "error": f"implausible zoomPercent {st.zoom_percent} in the status block; retry",
            }
        return m, None
    return FOV_MAGNIFICATION[st.fov_mode], None


def _aim_preconditions(cam: Camera, frame_w: float, frame_h: float) -> tuple[dict[str, object] | None, float, float, float]:
    """Shared guard chain for aim/fit: frame sanity, 16:9, wake, steady zoom,
    tracking off, magnification. Returns (refusal, magnification, yaw, pitch)."""
    if frame_w < 1 or frame_h < 1:
        return ({
            "ok": False,
            "error": (
                f"frame_width/frame_height must be >= 1 (got {frame_w}x{frame_h}) — "
                "pass the width/height from the snapshot's metadata block"
            ),
        }, 0.0, 0.0, 0.0)
    if not is_16_9(frame_w, frame_h):
        return ({
            "ok": False,
            "error": (
                f"frame {frame_w}x{frame_h} is not 16:9, but snapshots always return 16:9 "
                "frames — frame_width/frame_height look transposed or from another source"
            ),
        }, 0.0, 0.0, 0.0)
    if cam.ensure_awake():
        return ({
            "ok": False,
            "error": (
                "the camera was asleep and waking it moved the gimbal, so the frame you "
                "measured no longer matches where the camera points. Take a fresh snapshot and retry."
            ),
        }, 0.0, 0.0, 0.0)
    st, refusal = _steady_status(cam)
    if refusal:
        return refusal, 0.0, 0.0, 0.0
    assert st is not None
    if st.ai_mode == "unknown":
        return ({"ok": False, "error": "the status block did not decode (mode-switch transient); retry"}, 0.0, 0.0, 0.0)
    if st.ai_mode != "no-tracking":
        return ({
            "ok": False,
            "error": (
                f"AI tracking is active ({st.ai_mode}); it moves the gimbal itself and would "
                "fight the aim. Disable it with obsbot_ai_track enabled=false first."
            ),
        }, 0.0, 0.0, 0.0)
    magnification, refusal = _resolve_magnification(st)
    if refusal:
        return refusal, 0.0, 0.0, 0.0
    assert magnification is not None
    pose = cam.gimbal_get()
    return None, magnification, pose.yaw, pose.pitch


@mcp.tool(structured_output=False)
async def obsbot_snapshot(
    resolution: int = 640,
    quality: int = 80,
    settle_ms: int = 0,
):
    """Take a photo with the OBSBOT camera and return it as an image.

    resolution = longest edge in px (256-1920, default 640 — bigger costs the
    reader more tokens). quality = JPEG quality 1-100. settle_ms = wait before
    capturing; 0 is right for a static scene, 1200 covers a just-moved gimbal.
    A sleeping camera is woken automatically. The first frame is already sharp
    and well-exposed — extra delays buy nothing."""

    def work() -> object:
        resolution_c = max(256, min(1920, resolution))
        cam = Camera.find()
        cam.ensure_awake(settle_s=SNAPSHOT_WAKE_SETTLE_S)
        if settle_ms > 0:
            time.sleep(min(settle_ms, 15000) / 1000.0)
        snap = take_snapshot(cam.info.path, max_dim=resolution_c, quality=max(1, min(100, quality)))
        meta = {"width": snap.width, "height": snap.height, "attempts": snap.attempts}
        return [Image(data=snap.jpeg, format="jpeg"), json.dumps(meta)]

    return await _run(work)


@mcp.tool()
async def obsbot_status() -> dict[str, object]:
    """Camera state: presence, awake, AI-tracking mode, FOV/zoom, HDR,
    focus, and the last-commanded gimbal pose (open-loop on Linux — NOT a
    live reading; confirm pointing visually with a snapshot)."""

    def work() -> dict[str, object]:
        cam = Camera.find()
        return {"ok": True, "camera_present": True, **cam.status_dict()}

    return await _run(work)


@mcp.tool()
async def obsbot_wake() -> dict[str, object]:
    """Wake the camera (un-stows the gimbal to level — this MOVES the camera)."""

    def work() -> dict[str, object]:
        cam = Camera.find()
        woke = cam.ensure_awake()
        return {"ok": True, "state": "awake", "was_asleep": woke}

    return await _run(work)


@mcp.tool()
async def obsbot_sleep() -> dict[str, object]:
    """Put the camera to sleep (stows the lens face-down — the privacy pose)."""

    def work() -> dict[str, object]:
        Camera.find().sleep()
        return {"ok": True, "state": "sleep"}

    return await _run(work)


@mcp.tool()
async def obsbot_gimbal_move(yaw: float, pitch: float) -> dict[str, object]:
    """Move the gimbal to an absolute pose in degrees. +yaw pans camera-LEFT,
    +pitch tilts DOWN. Clamped to ±150 yaw / ±90 pitch. Returns once the
    command is sent; a large move takes about a second — snapshot with
    settle_ms=1200 to see the result."""

    def work() -> dict[str, object]:
        cam = Camera.find()
        cam.ensure_awake()
        pose = cam.gimbal_set(yaw, pitch)
        return {"ok": True, "yaw": pose.yaw, "pitch": pose.pitch,
                "clamped": pose.yaw != yaw or pose.pitch != pitch}

    return await _run(work)


@mcp.tool()
async def obsbot_gimbal_recenter() -> dict[str, object]:
    """Return the gimbal to center (yaw 0, pitch 0) — the known-good reset."""

    def work() -> dict[str, object]:
        cam = Camera.find()
        cam.ensure_awake()
        cam.gimbal_recenter()
        return {"ok": True, "yaw": 0, "pitch": 0}

    return await _run(work)


@mcp.tool()
async def obsbot_gimbal_position() -> dict[str, object]:
    """Last-COMMANDED gimbal pose in degrees. Linux cannot read the live pose
    (kernel caches the control), so this lies after tracking/wake/sleep moved
    the gimbal on its own — trust snapshots, not this, for where the camera
    actually points."""

    def work() -> dict[str, object]:
        pose = Camera.find().gimbal_get()
        return {"ok": True, "yaw": round(pose.yaw, 2), "pitch": round(pose.pitch, 2),
                "note": "last-commanded value, not a live reading"}

    return await _run(work)


@mcp.tool()
async def obsbot_zoom(ratio: float) -> dict[str, object]:
    """Set zoom (1.0 = widest, 2.0 = max). Waits up to 3s for the zoom to
    arrive; settled=false means still travelling, not failure."""

    def work() -> dict[str, object]:
        cam = Camera.find()
        cam.ensure_awake()
        actual, settled = cam.zoom_set(ratio)
        return {"ok": True, "ratio": actual, "settled": settled}

    return await _run(work)


@mcp.tool()
async def obsbot_ai_track(
    enabled: bool,
    mode: Literal[
        "normal", "upper-body", "close-up", "headless", "lower-body",
        "group", "whiteboard", "desk", "hand",
    ] = "normal",
) -> dict[str, object]:
    """Enable/disable AI subject tracking. Framing modes (normal/upper-body/
    close-up/headless/lower-body) track a human; scene modes (group/whiteboard/
    desk/hand) are their own tracking styles. While tracking is on the camera
    moves ITSELF and fights manual gimbal control. Verification polls up to 6s
    because mode switches park in a transient state for a few seconds."""

    def work() -> dict[str, object]:
        cam = Camera.find()
        cam.ensure_awake()
        verified, matched = cam.ai_track(enabled, mode)
        return {"ok": True, "enabled": enabled, "mode": mode,
                "verified": verified, "matched": matched}

    return await _run(work)


@mcp.tool()
async def obsbot_ai_track_speed(speed: Literal["standard", "sport"]) -> dict[str, object]:
    """Tracking responsiveness: standard (smooth) or sport (snappy)."""

    def work() -> dict[str, object]:
        Camera.find().track_speed(speed == "sport")
        return {"ok": True, "speed": speed}

    return await _run(work)


@mcp.tool()
async def obsbot_aim_at_pixel(
    x: float, y: float, frame_width: float, frame_height: float
) -> dict[str, object]:
    """Point the camera at a pixel from the MOST RECENT obsbot_snapshot.
    Converts image coordinates to a gimbal move using the measured FOV model
    and the current zoom. Refuses (with the reason) when the frame is stale
    or something else is moving the camera; follow the message and retry."""

    def work() -> dict[str, object]:
        cam = Camera.find()
        if not (0 <= x <= frame_width and 0 <= y <= frame_height):
            return {"ok": False, "error": f"pixel ({x},{y}) is outside the {frame_width}x{frame_height} frame"}
        refusal, magnification, cur_yaw, cur_pitch = _aim_preconditions(cam, frame_width, frame_height)
        if refusal:
            return refusal
        aim = aim_at_pixel(x, y, frame_width, frame_height, magnification, cur_yaw, cur_pitch)
        if aim.over_the_top:
            return {
                "ok": False,
                "error": (
                    "that target is past vertical from here — aiming would slew the long way "
                    "around. Tilt toward it first with obsbot_gimbal_move, snapshot, and re-aim."
                ),
            }
        cam.gimbal_set(aim.target_yaw, aim.target_pitch)
        return {
            "ok": True,
            "target": {"yaw": round(aim.target_yaw, 2), "pitch": round(aim.target_pitch, 2)},
            "offset": {"d_yaw": round(aim.d_yaw, 2), "d_pitch": round(aim.d_pitch, 2)},
            "clamped": aim.clamped,
            "previous": {"yaw": round(cur_yaw, 2), "pitch": round(cur_pitch, 2)},
        }

    return await _run(work)


@mcp.tool()
async def obsbot_zoom_to_fit(
    x: float, y: float, width: float, height: float,
    frame_width: float, frame_height: float, margin: float = 0.1,
) -> dict[str, object]:
    """Center and zoom onto a rectangular region of the MOST RECENT
    obsbot_snapshot (x,y = top-left corner in pixels). Moves the gimbal first,
    then zooms so the whole region fits with the given margin."""

    def work() -> dict[str, object]:
        cam = Camera.find()
        if margin < 0:
            return {"ok": False, "error": f"margin must be >= 0 (got {margin})"}
        if width <= 0 or height <= 0 or x < 0 or y < 0 or x + width > frame_width or y + height > frame_height:
            return {"ok": False, "error": f"region ({x},{y}) {width}x{height} is not inside the {frame_width}x{frame_height} frame"}
        refusal, magnification, cur_yaw, cur_pitch = _aim_preconditions(cam, frame_width, frame_height)
        if refusal:
            return refusal
        aim = aim_at_pixel(x + width / 2, y + height / 2, frame_width, frame_height,
                           magnification, cur_yaw, cur_pitch)
        if aim.over_the_top:
            return {
                "ok": False,
                "error": "the region center is past vertical from here — tilt toward it first, snapshot, retry",
            }
        required, fit_clamped = fit_magnification(magnification, frame_width, frame_height, width, height, margin)
        ratio = zoom_ratio_from_magnification(required)
        cam.gimbal_set(aim.target_yaw, aim.target_pitch)
        _, settled = cam.zoom_set(ratio)
        return {
            "ok": True,
            "target": {"yaw": round(aim.target_yaw, 2), "pitch": round(aim.target_pitch, 2)},
            "ratio": round(ratio, 3),
            "magnification": round(required, 3),
            "clamped": fit_clamped or aim.clamped,
            "settled": settled,
        }

    return await _run(work)


@mcp.tool()
async def obsbot_focus(
    mode: Literal["auto", "manual"],
    position: float = 50,
    face_priority: bool | None = None,
) -> dict[str, object]:
    """Focus control. auto = continuous autofocus (call before snapshots if
    sharpness matters). manual = fixed focus at position 0 (near) to 100
    (far). face_priority toggles face-priority autofocus."""

    def work() -> dict[str, object]:
        cam = Camera.find()
        result: dict[str, object] = {"ok": True, "mode": mode}
        if mode == "auto":
            cam.focus_auto()
        else:
            result["value"] = cam.focus_manual(max(0.0, min(100.0, position)))
            result["position"] = position
        if face_priority is not None:
            cam.face_focus(face_priority)
            result["face_priority"] = face_priority
        return result

    return await _run(work)


@mcp.tool()
async def obsbot_exposure(
    mode: Literal["auto", "manual"],
    level: float = 50,
    face_priority: bool | None = None,
) -> dict[str, object]:
    """Exposure control (vendor path — the standard UVC exposure control is a
    stub on this camera). level 0 (darkest) to 100 (brightest) applies in
    manual mode. face_priority weights auto-exposure for a face and is only
    valid with mode="auto" (the camera ignores the face-AE write otherwise)."""

    def work() -> dict[str, object]:
        cam = Camera.find()
        if mode == "manual" and face_priority is not None:
            return {
                "ok": False,
                "error": (
                    "face_priority applies only to auto exposure — the camera ignores the "
                    "face-AE setting in manual mode. Use mode=\"auto\" or omit face_priority."
                ),
            }
        raw = cam.exposure(mode == "manual", max(0.0, min(100.0, level)))
        result: dict[str, object] = {"ok": True, "mode": mode, "level": level, "raw": raw}
        if face_priority is not None:
            cam.face_ae(face_priority)
            result["face_priority"] = face_priority
        return result

    return await _run(work)


@mcp.tool()
async def obsbot_image_adjust(
    control: Literal["brightness", "contrast", "saturation", "hue", "sharpness"],
    level: float,
) -> dict[str, object]:
    """Set an image control as a percentage 0-100 of its device range."""

    def work() -> dict[str, object]:
        value = Camera.find().image_adjust(control, max(0.0, min(100.0, level)))
        return {"ok": True, "control": control, "level": level, "value": value}

    return await _run(work)


@mcp.tool()
async def obsbot_image_mode(
    hdr: bool | None = None,
    fov: Literal["wide", "medium", "narrow"] | None = None,
    white_balance: Literal["auto", "manual"] | None = None,
    wb_temperature_k: int = 5000,
) -> dict[str, object]:
    """Set image modes; only the parameters you pass are changed. fov selects
    the discrete field-of-view (wide 1x / medium 1.15x / narrow 1.47x — the
    continuous zoom writes to the same scale). white_balance manual uses
    wb_temperature_k (Kelvin)."""

    def work() -> dict[str, object]:
        cam = Camera.find()
        result: dict[str, object] = {"ok": True}
        if hdr is not None:
            cam.hdr(hdr)
            result["hdr"] = hdr
        if fov is not None:
            cam.fov(fov)
            result["fov"] = fov
        if white_balance is not None:
            value = cam.white_balance(None if white_balance == "auto" else wb_temperature_k)
            result["white_balance"] = white_balance
            if value is not None:
                result["wb_temperature_k"] = value
        if len(result) == 1:
            result["note"] = "no parameters given; nothing changed"
        return result

    return await _run(work)


@mcp.tool()
async def obsbot_record_clip(duration_s: float = 10, audio: bool = True) -> dict[str, object]:
    """Record a video clip (1080p H.264, max 60s) to ~/Videos/OBSBOT/. Blocks
    for the duration. Uses the camera's mic when available; silent otherwise."""

    def work() -> dict[str, object]:
        cam = Camera.find()
        cam.ensure_awake(settle_s=SNAPSHOT_WAKE_SETTLE_S)
        clip = record_clip(cam.info.path, min(duration_s, MAX_CLIP_SECONDS), audio)
        result: dict[str, object] = {"ok": True, "path": clip.path, "duration_s": clip.duration_s,
                                     "size_bytes": clip.size_bytes, "audio": clip.audio}
        if audio and not clip.audio:
            result["note"] = "camera mic unavailable (no /dev/snd permission, or device busy); clip is silent"
        return result

    return await _run(work)


@mcp.tool()
async def obsbot_preview(action: Literal["start", "stop"]) -> dict[str, object]:
    """Open/close a live preview window on the desktop (for a human watching).
    NOTE: the preview holds the camera stream, so snapshots and clips fail
    while it is open — stop it before capturing."""

    def work() -> dict[str, object]:
        if action == "start":
            cam = Camera.find()
            cam.ensure_awake()
            _preview.start(cam.info.path)
            return {"ok": True, "action": "start",
                    "note": "preview holds the stream; stop it before snapshots"}
        stopped = _preview.stop()
        return {"ok": True, "action": "stop", "was_running": stopped}

    return await _run(work)


def main() -> None:
    """json_response: single JSON-RPC replies come back as plain JSON instead
    of an SSE stream, so naive agents can drive the server with bare HTTP
    POSTs; MCP-native clients accept both per the streamable-http spec."""
    mcp.run("streamable-http", host=HOST, port=PORT,
            stateless_http=True, json_response=True)


if __name__ == "__main__":
    main()
