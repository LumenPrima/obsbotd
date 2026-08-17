"""Hotplug-aware camera discovery.

There is deliberately no udev watcher and no cached device handle: every tool
call re-enumerates /dev/video* (a handful of opens + ioctls, well under 1ms).
Unplugging the camera therefore needs no event handling at all — the next call
simply finds nothing and reports the camera as unavailable.
"""

from __future__ import annotations

import glob
import os
import errno
from dataclasses import dataclass

from .uvc import VideoDevice, UVC_GET_LEN


class CameraUnavailable(Exception):
    """The camera is unplugged or otherwise not present right now."""


@dataclass(frozen=True)
class CameraInfo:
    path: str
    card: str
    xu_unit: int
    xu_selector_len: int


def _probe_xu_unit(dev: VideoDevice, selector: int) -> tuple[int, int] | None:
    """Find the vendor Extension Unit by asking each plausible unit id for the
    control length of `selector`. Non-XU units answer EINVAL/ENOENT."""
    for unit in range(1, 9):
        try:
            data = bytearray(2)
            dev.xu_query(unit, selector, UVC_GET_LEN, data)
            length = int.from_bytes(data, "little")
            if length > 0:
                return unit, length
        except OSError:
            continue
    return None


def find_camera(name_match: str = "OBSBOT", xu_selector: int = 2) -> CameraInfo:
    """Locate the first OBSBOT capture node and its vendor XU unit.

    Raises CameraUnavailable when no matching camera is plugged in — callers
    turn that into a friendly "camera is currently unplugged" tool result.
    """
    candidates = sorted(glob.glob("/dev/video*"))
    saw_camera_without_xu = False
    for path in candidates:
        try:
            with VideoDevice(path) as dev:
                if not dev.is_capture_node():
                    continue
                card = dev.card_name()
                if name_match.lower() not in card.lower():
                    continue
                probed = _probe_xu_unit(dev, xu_selector)
                if probed is None:
                    saw_camera_without_xu = True
                    continue
                unit, length = probed
                return CameraInfo(path=path, card=card, xu_unit=unit, xu_selector_len=length)
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ENODEV, errno.EBUSY, errno.EACCES):
                continue
            continue
    if saw_camera_without_xu:
        raise CameraUnavailable(
            "an OBSBOT capture node exists but its vendor extension unit did not "
            "answer — the camera may be resetting; retry in a moment"
        )
    raise CameraUnavailable(
        "no OBSBOT camera is currently connected (it may be unplugged)"
    )


def camera_present() -> bool:
    try:
        find_camera()
        return True
    except CameraUnavailable:
        return False


def video_nodes() -> list[str]:
    return sorted(p for p in glob.glob("/dev/video*") if os.path.exists(p))
