"""Direct V4L2/UVC access via ctypes ioctls.

Everything here is stable kernel ABI (videodev2.h, uvcvideo.h). Ioctl request
numbers are computed from struct sizes at import time rather than hardcoded, so
a struct-definition mistake fails loudly (ENOTTY) instead of corrupting memory.
"""

from __future__ import annotations

import os
import ctypes
import fcntl
from dataclasses import dataclass

_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction: int, ioc_type: str, nr: int, size: int) -> int:
    return (
        (direction << _IOC_DIRSHIFT)
        | (ord(ioc_type) << _IOC_TYPESHIFT)
        | (nr << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


class V4l2Capability(ctypes.Structure):
    _fields_ = (
        ("driver", ctypes.c_uint8 * 16),
        ("card", ctypes.c_uint8 * 32),
        ("bus_info", ctypes.c_uint8 * 32),
        ("version", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("device_caps", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 3),
    )


class V4l2ExtControl(ctypes.Structure):
    _pack_ = 1
    _fields_ = (
        ("id", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("reserved2", ctypes.c_uint32),
        ("value", ctypes.c_int32),
        ("_pad", ctypes.c_int32),
    )


class V4l2ExtControls(ctypes.Structure):
    _fields_ = (
        ("which", ctypes.c_uint32),
        ("count", ctypes.c_uint32),
        ("error_idx", ctypes.c_uint32),
        ("request_fd", ctypes.c_int32),
        ("reserved", ctypes.c_uint32),
        ("controls", ctypes.POINTER(V4l2ExtControl)),
    )


class V4l2Queryctrl(ctypes.Structure):
    _fields_ = (
        ("id", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("name", ctypes.c_char * 32),
        ("minimum", ctypes.c_int32),
        ("maximum", ctypes.c_int32),
        ("step", ctypes.c_int32),
        ("default_value", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 2),
    )


class UvcXuControlQuery(ctypes.Structure):
    _fields_ = (
        ("unit", ctypes.c_uint8),
        ("selector", ctypes.c_uint8),
        ("query", ctypes.c_uint8),
        ("size", ctypes.c_uint16),
        ("data", ctypes.POINTER(ctypes.c_uint8)),
    )


VIDIOC_QUERYCAP = _ioc(_IOC_READ, "V", 0, ctypes.sizeof(V4l2Capability))
VIDIOC_QUERYCTRL = _ioc(_IOC_READ | _IOC_WRITE, "V", 36, ctypes.sizeof(V4l2Queryctrl))
VIDIOC_G_EXT_CTRLS = _ioc(_IOC_READ | _IOC_WRITE, "V", 71, ctypes.sizeof(V4l2ExtControls))
VIDIOC_S_EXT_CTRLS = _ioc(_IOC_READ | _IOC_WRITE, "V", 72, ctypes.sizeof(V4l2ExtControls))
UVCIOC_CTRL_QUERY = _ioc(_IOC_READ | _IOC_WRITE, "u", 0x21, ctypes.sizeof(UvcXuControlQuery))

V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_DEVICE_CAPS = 0x80000000
V4L2_CTRL_WHICH_CUR_VAL = 0

UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81
UVC_GET_MIN = 0x82
UVC_GET_MAX = 0x83
UVC_GET_LEN = 0x85
UVC_GET_INFO = 0x86

CID_CAMERA_BASE = 0x009A0900
CID_EXPOSURE_AUTO = CID_CAMERA_BASE + 1
CID_EXPOSURE_ABSOLUTE = CID_CAMERA_BASE + 2
CID_PAN_ABSOLUTE = CID_CAMERA_BASE + 8
CID_TILT_ABSOLUTE = CID_CAMERA_BASE + 9
CID_FOCUS_ABSOLUTE = CID_CAMERA_BASE + 10
CID_FOCUS_AUTO = CID_CAMERA_BASE + 12
CID_ZOOM_ABSOLUTE = CID_CAMERA_BASE + 13

CID_USER_BASE = 0x00980900
CID_BRIGHTNESS = CID_USER_BASE + 0
CID_CONTRAST = CID_USER_BASE + 1
CID_SATURATION = CID_USER_BASE + 2
CID_HUE = CID_USER_BASE + 3
CID_AUTO_WHITE_BALANCE = CID_USER_BASE + 12
CID_WHITE_BALANCE_TEMPERATURE = CID_USER_BASE + 26
CID_SHARPNESS = CID_USER_BASE + 27


@dataclass(frozen=True)
class ControlRange:
    minimum: int
    maximum: int
    step: int
    default: int


class VideoDevice:
    """One open V4L2 device fd. Use as a context manager; open/close is cheap
    (microseconds), so callers open per operation — holding no fd between calls
    is what makes hotplug handling trivial."""

    def __init__(self, path: str):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "VideoDevice":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def query_cap(self) -> V4l2Capability:
        cap = V4l2Capability()
        fcntl.ioctl(self.fd, VIDIOC_QUERYCAP, cap)
        return cap

    def is_capture_node(self) -> bool:
        cap = self.query_cap()
        caps = (
            cap.device_caps
            if cap.capabilities & V4L2_CAP_DEVICE_CAPS
            else cap.capabilities
        )
        return bool(caps & V4L2_CAP_VIDEO_CAPTURE)

    def card_name(self) -> str:
        return bytes(self.query_cap().card).split(b"\0", 1)[0].decode(errors="replace")

    def query_control_range(self, cid: int) -> ControlRange:
        q = V4l2Queryctrl(id=cid)
        fcntl.ioctl(self.fd, VIDIOC_QUERYCTRL, q)
        return ControlRange(q.minimum, q.maximum, q.step, q.default_value)

    def get_controls(self, cids: list[int]) -> list[int]:
        arr = (V4l2ExtControl * len(cids))()
        for i, cid in enumerate(cids):
            arr[i].id = cid
        ctrls = V4l2ExtControls(
            which=V4L2_CTRL_WHICH_CUR_VAL, count=len(cids), controls=arr
        )
        fcntl.ioctl(self.fd, VIDIOC_G_EXT_CTRLS, ctrls)
        return [arr[i].value for i in range(len(cids))]

    def set_controls(self, pairs: list[tuple[int, int]]) -> None:
        """Set several controls in ONE VIDIOC_S_EXT_CTRLS ioctl. This is load-
        bearing for pan+tilt: uvcvideo's per-control path does a read-modify-
        write of the shared PANTILT_ABSOLUTE register, so two separate calls
        cancel half the move. One atomic call avoids that."""
        arr = (V4l2ExtControl * len(pairs))()
        for i, (cid, value) in enumerate(pairs):
            arr[i].id = cid
            arr[i].value = value
        ctrls = V4l2ExtControls(
            which=V4L2_CTRL_WHICH_CUR_VAL, count=len(pairs), controls=arr
        )
        fcntl.ioctl(self.fd, VIDIOC_S_EXT_CTRLS, ctrls)

    def xu_query(self, unit: int, selector: int, query: int, data: bytearray) -> None:
        buf = (ctypes.c_uint8 * len(data)).from_buffer(data)
        q = UvcXuControlQuery(
            unit=unit,
            selector=selector,
            query=query,
            size=len(data),
            data=ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8)),
        )
        fcntl.ioctl(self.fd, UVCIOC_CTRL_QUERY, q)

    def xu_get_len(self, unit: int, selector: int) -> int:
        data = bytearray(2)
        self.xu_query(unit, selector, UVC_GET_LEN, data)
        return int.from_bytes(data, "little")

    def xu_set(self, unit: int, selector: int, payload: bytes) -> None:
        self.xu_query(unit, selector, UVC_SET_CUR, bytearray(payload))

    def xu_get(self, unit: int, selector: int, size: int) -> bytes:
        data = bytearray(size)
        self.xu_query(unit, selector, UVC_GET_CUR, data)
        return bytes(data)
