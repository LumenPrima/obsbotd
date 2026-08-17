import struct

import pytest

from obsbotd.protocol import (
    build_frame,
    build_get_frame,
    crc16_usb,
    decode_status,
    encode_ai_mode,
    encode_exposure,
    encode_run_status,
    parse_frame,
    percent_to_range,
    zoom_ratio_to_units,
    FrameParseError,
)


def test_crc_check_constant():
    assert crc16_usb(b"123456789") == 0xB4C8


def test_set_frame_matches_js_codec_golden():
    f = build_frame((0x6444, 0x04), 1, struct.pack("<fff", 0.0, -10.0, 30.0))
    golden = bytes.fromhex(
        "aa2501000c009b4a0a0444640c00fa8b00000000000020c10000f041"
    )
    assert f[: len(golden)] == golden
    assert len(f) == 60 and set(f[28:]) == {0}


def test_get_frame_matches_js_codec_golden():
    g = build_get_frame((0x3884, 0x04), 2)
    assert g[:12] == bytes.fromhex("aa0102000c00858c0a048438")


def test_parse_roundtrip():
    f = build_frame((0x6444, 0x04), 7, b"\x01\x02\x03")
    p = parse_frame(f)
    assert (p.seq, p.cmd, p.receiver, p.payload) == (7, 0x6444, 0x04, b"\x01\x02\x03")


def test_parse_rejects_corruption():
    f = bytearray(build_frame((0x6444, 0x04), 1, b"\x01"))
    f[16] ^= 0xFF
    with pytest.raises(FrameParseError):
        parse_frame(bytes(f))
    with pytest.raises(FrameParseError):
        parse_frame(b"\xab" + bytes(f[1:]))


def test_run_status_polarity():
    assert encode_run_status(True) == b"\x00\x00\x00\x00"
    assert encode_run_status(False) == b"\x01\x00\x00\x00"


def test_exposure_payload_is_five_bytes():
    p = encode_exposure(True, 1250)
    assert len(p) == 5 and p[0] == 1
    assert struct.unpack("<I", p[1:])[0] == 1250


def test_ai_mode_buffer():
    b = encode_ai_mode("human", "upper-body")
    assert (b[0], b[1], b[2], b[3]) == (0x16, 0x02, 2, 1) and len(b) == 60
    assert encode_ai_mode("desk")[2] == 5 and encode_ai_mode("desk")[3] == 0


def test_status_decode_live_sample():
    block = bytes.fromhex(
        "2701000200000001010178000000013800000000010021000001030000"
        "00001e000310000000000000000000000000000000000000000000000000"
        "0000"
    )[:60]
    st = decode_status(block)
    assert st.awake is True and st.ai_mode == "no-tracking"
    assert st.fov_mode == "wide" and st.track_speed == "standard"


def test_status_m6_transient_is_unknown():
    block = bytearray(60)
    block[0x18] = 6
    assert decode_status(bytes(block)).ai_mode == "unknown"


def test_scale_helpers():
    assert zoom_ratio_to_units(1.0, 0, 100) == 0
    assert zoom_ratio_to_units(2.0, 0, 100) == 100
    assert percent_to_range(50, 1, 2500) == 1250
