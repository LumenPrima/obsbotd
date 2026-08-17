# OBSBOT Tiny 2 Vendor Wire Protocol — Reimplementation Specification

Extracted from `obsbot-mcp` `dist/codec/{frame,crc,encoding,opcodes,commands,preset,types}.js`. All multi-byte integers on the wire are **little-endian** unless stated otherwise. All hex values below are literal from source.

The protocol has **two distinct transports**:

1. **"V3" framed vendor commands** — 60-byte framed messages with CRCs (sections 1–3, 5, 7), carried over the vendor channel.
2. **Raw UVC extension-unit (XU) selector reads/writes** — flat fixed-offset 60-byte buffers with **no magic, no CRC, no framing** (sections 6, 8, 9): selector 6 (settings writes + status read), selector 12 (preset list), selector 13 (preset entry walk).

---

## 1. V3 Frame Layout

`buildFrame(o)` always produces a **fixed 60-byte, zero-padded buffer**.

| Offset | Width | Field | Value / encoding |
|---|---|---|---|
| 0 | 1 | magic | `0xAA` (parser rejects anything else) |
| 1 | 1 | flags | `0x25` = SET (nested payload present; the default); `0x01` = header-only GET |
| 2–3 | 2 | seq | u16le sequence number, caller-supplied |
| 4–5 | 2 | len | u16le, **hard-coded constant `12`** ("header covered by token") — it does NOT reflect payload length |
| 6–7 | 2 | header token | u16le CRC-16/USB over `frame[0..12)` computed **with bytes 6–7 zeroed**, then written here |
| 8 | 1 | sender | default `0x0A` |
| 9 | 1 | receiver | subsystem address from the opcode table (e.g. `0x02` Camera, `0x04` Ai) |
| 10–11 | 2 | cmd | u16le wire command (the opcode table's `wireCmd`) |
| 12–13 | 2 | len2 | u16le payload byte length — **only present when payload length > 0**; otherwise bytes 12+ stay zero |
| 14–15 | 2 | token2 | u16le CRC-16/USB over the segment `frame[12 .. 12+len2+4)` (i.e. len2(2) + token2(2) + payload(len2)) computed **with bytes 14–15 zeroed**, then written here |
| 16 … 16+len2−1 | len2 | payload | command-specific bytes |
| … 59 | — | padding | zeros out to the fixed 60-byte buffer |

**Parsing** (`parseFrame`), with `FrameParseError` on any failure:
1. Length < 12 → error `frame too short`.
2. `buf[0] !== 0xAA` → error `bad magic`.
3. Copy `buf[0..12)`, zero bytes 6–7, compute CRC-16/USB; must equal `buf.readUInt16LE(6)` → else `header CRC mismatch`.
4. Extract `seq = u16le@2`, `sender = buf[8]`, `receiver = buf[9]`, `cmd = u16le@10`.
5. Payload exists only if `buf.length >= 16` **and** `len2 = u16le@12` is > 0. Then `end = 16 + len2`; if `buf.length < end` → `truncated payload`. Copy `buf[12..end)`, zero its bytes 2–3 (frame offsets 14–15), CRC must equal `buf.readUInt16LE(14)` → else `payload CRC mismatch`. Payload = `buf[16..end)`.
6. Missing/zero-length segment ⇒ empty payload.

**GET quirk (hardware-verified):** the framed vendor **GET** path only answers when `frame[1] = 0x01` (header-only, no nested payload). SETs use `0x25`. GETs sent with the SET flags byte are silently unanswered. GET replies come back as frames whose payload holds the state.

### Worked frame examples (generated with the actual codec)

`AI_SET_GIM_MOTOR_DEG` (cmd `0x6444`, receiver `0x04`), seq=1, yaw=30°, pitch=−10°, roll=0°:

```
aa 25 0100 0c00 9b4a 0a 04 4464 | 0c00 fa8b | 00000000 000020c1 0000f041 | 00…00 (pad to 60)
```
— header token `0x4A9B`, payload token `0x8BFA`, payload = f32le(roll=0), f32le(pitch=−10), f32le(yaw=30).

`AI_GET_GIM_BOOT_POS` (cmd `0x3884`, receiver `0x04`), seq=2, flags=0x01, no payload:

```
aa 01 0200 0c00 858c 0a 04 8438 | 00…00 (pad to 60)
```
— header token `0x8C85`.

---

## 2. CRC — CRC-16/USB

```
poly (reflected): 0xA001   (normal form 0x8005)
init:             0xFFFF
refin/refout:     true (impl is naturally reflected: LSB-first, right shifts)
xorout:           0xFFFF
width:            16 bits, result masked & 0xFFFF
```

Reference algorithm (from `crc.js`):
```
crc = 0xFFFF
for each byte b in data:
    crc ^= b
    repeat 8 times:
        crc = (crc & 1) ? (crc >> 1) ^ 0xA001 : (crc >> 1)
return (crc ^ 0xFFFF) & 0xFFFF
```

**Coverage:** Header CRC covers frame bytes `[0,12)` with the token field (bytes 6–7) treated as zero. Payload CRC covers frame bytes `[12, 16+len2)` — i.e. len2 + token2 + payload — with the token2 field (bytes 14–15) treated as zero.

**Worked check value:** `crc16usb("123456789")` = **`0xB4C8`** (the standard CRC-16/USB check constant — confirms the implementation).

---

## 3. Opcode Table

Each opcode row has: `name`, `subsystem`, `set` (subsystem id), `cmdId` (per-subsystem index), `wireCmd` (the u16le placed at frame offset 10–11; `null` = not sendable over V3), `receiver` (frame offset 9; `null` when not sendable). The frame's `cmd` field is **always `wireCmd`**, never `cmdId`. 444 commands total, 437 with a resolvable wireCmd.

Subsystem `set` id → default `receiver`:

| Subsystem | `set` | `receiver` |
|---|---|---|
| Camera | 0x01 | 0x02 |
| Gimbal | 0x02 | 0x03 |
| Ai | 0x03 | 0x04 |
| Upgrade | 0x05 | 0x0D |
| Route | 0x06 | 0x01 |
| Factory | 0x0B | 0x0E |
| PrimaryBle | 0x0C | 0x13 |
| SysMg | 0x0D | 0x12 |
| RemoteBle | 0x0E | 0x14 |
| TXBle | 0x0F | 0x18 |

### 3a. Full wireCmd map (name → wireCmd, receiver). `null` wireCmd = not sendable.

**Camera (receiver 0x02):**

| name | cmdId | wireCmd |
|---|---|---|
| CAM_SET_DEV_STATUS | 0x02 | 0xA0C2 |
| CAM_SET_FACE_FOCUS | 0x03 | 0x3602 |
| CAM_GET_FACE_FOCUS | 0x04 | 0x35C2 |
| CAM_SET_EXPOSURE_TINY2 | 0x05 | 0x2982 |
| CAM_GET_EXPOSURE_TINY2 | 0x06 | 0x2942 |
| CAM_GET_EXPOSURE_RANGE_TINY2 | 0x07 | 0x29C2 |
| CAM_GET_WHITEBALANCE_OFFSET | 0x08 | 0x2A02 |
| CAM_SET_WHITEBALANCE_OFFSET | 0x09 | 0x2A42 |
| CAM_GET_WHITEBALANCE_SETTING | 0x0A | 0x2A82 |
| CAM_SET_WHITEBALANCE_SETTING | 0x0B | 0x2AC2 |
| CAM_GET_WHITEBALANCE_RANGE | 0x0C | 0x2B02 |
| CAM_SET_TAKE_PHOTOS | 0x0D | 0x0102 |
| CAM_SET_VIDEO_RECORD | 0x0E | 0x0142 |
| CAM_SET_LAPSE_DELAY_TIME | 0x0F | 0x0202 |
| CAM_GET_LAPSE_DELAY_TIME | 0x10 | 0x01C2 |
| CAM_SET_LAPSE_DELAY_CANCEL | 0x11 | 0x0282 |
| CAM_NTY_NEW_MEDIA_FILE | 0x12 | 0x0182 |
| CAM_GET_WDR_MODE | 0x13 | 0x02C2 |
| CAM_SET_WDR_MODE | 0x14 | 0x0302 |
| CAM_GET_WDR_MODE_LIST | 0x15 | 0x0342 |
| CAM_SET_BOOT_STATE | 0x16 | 0x03C2 |
| CAM_GET_BOOT_STATE | 0x17 | 0x0382 |
| CAM_SET_PHOTO_QUALITY | 0x18 | 0x05C2 |
| CAM_SET_PHOTO_FORMAT | 0x19 | 0x0642 |
| CAM_SET_RECORD_RESOLUTION | 0x1A | 0x0842 |
| CAM_GET_RECORD_SPLIT_SIZE | 0x1B | 0x0A02 |
| CAM_SET_RECORD_SPLIT_SIZE | 0x1C | 0x0A42 |
| CAM_GET_MAIN_VIDEO_FORMAT | 0x1D | 0x0B82 |
| CAM_SET_MAIN_VIDEO_FORMAT | 0x1E | 0x0BC2 |
| CAM_GET_MAIN_VIDEO_BIT_LEVEL | 0x1F | 0x0C42 |
| CAM_SET_MAIN_VIDEO_BIT_LEVEL | 0x20 | 0x0C82 |
| CAM_GET_MODULE_ACTIVATE | 0x22 | 0x0E42 |
| CAM_SET_KCP_PREVIEW_RESOLUTION | 0x23 | 0x1042 |
| CAM_SET_NDI_RTSP_RESOLUTION | 0x24 | 0x1102 |
| CAM_GET_NDI_RTSP_BIT_LEVEL | 0x25 | 0x1182 |
| CAM_SET_NDI_RTSP_BIT_LEVEL | 0x26 | 0x11C2 |
| CAM_GET_NDI_RTSP_FORMAT | 0x27 | 0x1242 |
| CAM_SET_NDI_RTSP_FORMAT | 0x28 | 0x1282 |
| CAM_GET_NDI_ENABLE | 0x29 | 0x1302 |
| CAM_SET_NDI_ENABLE | 0x2A | 0x1342 |
| CAM_SET_NDI_BOOT_ENABLE | 0x2B | 0x13C2 |
| CAM_NTY_LIVE_STREAM_STATUS | 0x2C | 0x1402 |
| CAM_GET_MIRROR_FLIP | 0x2D | 0x1802 |
| CAM_SET_MIRROR_FLIP | 0x2E | 0x1842 |
| CAM_GET_ROTATION_DEG | 0x2F | 0x1882 |
| CAM_SET_ROTATION_DEG | 0x30 | 0x18C2 |
| CAM_GET_ZOOM_CTRL_INFO | 0x31 | 0x1902 |
| CAM_SET_ZOOM_ABSOLUTE | 0x32 | 0x1942 |
| CAM_SET_ZOOM_RELATIVE | 0x33 | 0x1982 |
| CAM_SET_ZOOM_STOP | 0x34 | 0x19C2 |
| CAM_SET_ROI_CTRL | 0x35 | 0x1A02 |
| CAM_GET_HDMI_INFO | 0x36 | 0x1AC2 |
| CAM_SET_HDMI_INFO | 0x37 | 0x1B02 |
| CAM_GET_WATER_MARK | 0x38 | 0x1BC2 |
| CAM_SET_WATER_MARK | 0x39 | 0x1B82 |
| CAM_SET_MEDIA_PARAMETER | 0x3A | 0x06C2 |
| CAM_GET_MEDIA_PARAMETER | 0x3B | 0x0702 |
| CAM_GET_RANGE_CONFIG | 0x3C | 0x0742 |
| CAM_GET_WHITE_BALANCE | 0x3D | 0x2042 |
| CAM_SET_WHITE_BALANCE | 0x3E | 0x2082 |
| CAM_GET_WHITE_BALANCE_LIST | 0x3F | 0x20C2 |
| CAM_GET_ISO_LIMIT | 0x40 | 0x2142 |
| CAM_SET_ISO_LIMIT | 0x41 | 0x2182 |
| CAM_GET_AE_LOCK | 0x42 | 0x21C2 |
| CAM_SET_AE_LOCK | 0x43 | 0x2202 |
| CAM_GET_FACE_AE | 0x44 | 0x2242 |
| CAM_SET_FACE_AE | 0x45 | 0x2282 |
| CAM_GET_EXPOSURE_MODE | 0x46 | 0x2402 |
| CAM_SET_EXPOSURE_MODE | 0x47 | 0x2442 |
| CAM_GET_GAMMA_USER_MODE | 0x48 | 0x3082 |
| CAM_SET_GAMMA_USER_MODE | 0x49 | 0x30C2 |
| CAM_GET_P_AE_EVBIAS | 0x4A | 0x24C2 |
| CAM_SET_P_AE_EVBIAS | 0x4B | 0x2502 |
| CAM_SET_S_AE_EVBIAS | 0x4C | 0x2582 |
| CAM_GET_S_AE_SHUTTER | 0x4D | 0x25C2 |
| CAM_SET_S_AE_SHUTTER | 0x4E | 0x2602 |
| CAM_GET_A_AE_EVBIAS | 0x4F | 0x2642 |
| CAM_SET_A_AE_EVBIAS | 0x50 | 0x2682 |
| CAM_SET_A_AE_APERTURE | 0x51 | 0x2702 |
| CAM_GET_M_AE_SHUTTER | 0x52 | 0x2742 |
| CAM_SET_M_AE_SHUTTER | 0x53 | 0x2782 |
| CAM_SET_M_AE_APERTURE | 0x54 | 0x2802 |
| CAM_GET_M_AE_ISO | 0x55 | 0x2842 |
| CAM_SET_M_AE_ISO | 0x56 | 0x2882 |
| CAM_SET_M_ANTI_FLICK | 0x57 | 0x2902 |
| CAM_SET_IMAGE_STYLE | 0x58 | 0x2EC2 |
| CAM_SET_BRIGHTNESS | 0x59 | 0x2C42 |
| CAM_SET_CONTRAST | 0x5A | 0x2CC2 |
| CAM_SET_HUE | 0x5B | 0x2D42 |
| CAM_SET_SATURATION | 0x5C | 0x2DC2 |
| CAM_SET_SHARP | 0x5D | 0x2E42 |
| CAM_GET_AUTO_FOCUS | 0x5E | 0x3402 |
| CAM_SET_AUTO_FOCUS | 0x5F | 0x3442 |
| CAM_GET_FOCUS_POS | 0x60 | 0x34C2 |
| CAM_SET_FOCUS_POS | 0x61 | 0x3502 |
| CAM_SET_USER_FOCUS_POS | 0x62 | 0x3582 |
| CAM_GET_AFC_TYPE | 0x63 | 0x35C2 |
| CAM_SET_AFC_TYPE | 0x64 | 0x3602 |
| CAM_GET_GAMMA_MODE | 0x65 | 0x2F42 |
| CAM_SET_GAMMA_MODE | 0x66 | 0x2F82 |
| CAM_GET_NIGHT_MODE | 0x67 | 0x3002 |
| CAM_SET_NIGHT_MODE | 0x68 | 0x3042 |
| CAM_GET_IQ_IMG_STYLE_MODE | 0x69 | 0x3802 |
| CAM_SET_IQ_IMG_STYLE_MODE | 0x6A | 0x3842 |
| CAM_GET_IQ_PARAMETER | 0x6B | 0x3882 |
| CAM_SET_IQ_PARAMETER | 0x6C | 0x38C2 |
| CAM_GET_ISP_PARAMETER | 0x6D | 0x3D02 |
| CAM_SET_ISP_PARAMETER | 0x6E | 0x3D42 |
| CAM_GET_ISO_THRESHOLD | 0x6F | 0x3D82 |
| CAM_SET_ISO_THRESHOLD | 0x70 | 0x3DC2 |
| CAM_GET_AUDIO_VOLUME | 0x71 | 0x80C2 |
| CAM_SET_AUDIO_VOLUME | 0x72 | 0x8102 |
| CAM_GET_AUDIO_VQE | 0x73 | 0x8142 |
| CAM_SET_AUDIO_VQE | 0x74 | 0x8182 |
| CAM_SET_AUDIO_AGC | 0x75 | 0x8202 |
| CAM_GET_AUDIO_AGC_INTEGRAL | 0x76 | 0x8242 |
| CAM_SET_AUDIO_AGC_INTEGRAL | 0x77 | 0x8282 |
| CAM_GET_AUDIO_NOISE_REDUCTION | 0x78 | 0x82C2 |
| CAM_SET_AUDIO_NOISE_REDUCTION | 0x79 | 0x8302 |
| CAM_GET_AUDIO_AUX_TYPE | 0x7A | 0x8802 |
| CAM_SET_AUDIO_AUX_TYPE | 0x7B | 0x8842 |
| CAM_GET_AUDIO_SOURCE_SELECT | 0x7C | 0x8982 |
| CAM_SET_AUDIO_SOURCE_SELECT | 0x7D | 0x8942 |
| CAM_GET_AUDIO_MUTE | 0x7E | 0x89C2 |
| CAM_SET_AUDIO_MUTE | 0x7F | 0x8A02 |
| CAM_GET_AUDIO_SOURCE | 0x80 | 0x8A42 |
| CAM_GET_AUDIO_SELECT | 0x81 | 0x8A82 |
| CAM_SET_AUDIO_SELECT | 0x82 | 0x8AC2 |
| CAM_GET_USB_MODE | 0x83 | 0x6042 |
| CAM_SET_USB_MODE | 0x84 | 0x6082 |
| CAM_GET_SD_STATUS | 0x86 | 0x6482 |
| CAM_GET_RECORD_EXT_SETTING | 0x87 | 0x6D02 |
| CAM_SET_RECORD_EXT_SETTING | 0x88 | 0x6D42 |
| CAM_SET_SYS_TIME | 0x89 | 0xA082 |
| CAM_GET_SYS_TIME | 0x8A | 0xA042 |
| CAM_GET_SUSPEND_TIME | 0x8C | 0xA182 |
| CAM_SET_SUSPEND_TIME | 0x8D | 0xA1C2 |
| CAM_GET_PLUG | 0x8E | 0xA302 |
| CAM_SET_PLUG | 0x8F | 0xA342 |
| CAM_SET_PREPARE_LOG | 0x90 | 0xAA02 |
| CAM_SET_SCHEDULED_SLEEP | 0x91 | 0xA242 |
| CAM_SET_SCHEDULED_RESUME | 0x92 | 0xA2C2 |
| CAM_GET_SCHEDULED_SLEEP_UVC | 0x93 | 0xA382 |
| CAM_SET_SCHEDULED_SLEEP_UVC | 0x94 | 0xA3C2 |
| CAM_GET_SCHEDULED_RESUME_UVC | 0x95 | 0xA402 |
| CAM_SET_SCHEDULED_RESUME_UVC | 0x96 | 0xA442 |
| CAM_GET_FIELD_VIEW | 0x97 | 0xAB02 |
| CAM_SET_VIRTUAL_TRACK_INFO | 0x98 | 0xAC02 |
| CAM_GET_VIRTUAL_TRACK_INFO | 0x99 | 0xAC42 |
| CAM_SET_VIRTUAL_TRACK_GESTURE | 0x9A | 0xAC82 |
| CAM_GET_VIRTUAL_TRACK_GESTURE | 0x9B | 0xACC2 |
| CAM_SET_MODULE_PARAMETER | 0x9C | 0x60C2 |
| CAM_GET_MODULE_PARAMETER | 0x9D | 0x6102 |
| CAM_SET_REMOTE_CUSTOM_KEY | 0x9E | 0x6842 |
| CAM_GET_REMOTE_CUSTOM_KEY | 0x9F | 0x6882 |
| CAM_GET_CDC_CAPABILITY | 0xA0 | 0xAD02 |
| CAM_GET_CDC_NOTIFY_STATUS | 0xA1 | 0xAD82 |
| CAM_SET_CDC_NOTIFY_STATUS | 0xA2 | 0xADC2 |
| CAM_SET_VIRTUAL_TRACK_ENABLE | 0xA3 | 0xAE82 |
| CAM_GET_VIRTUAL_TRACK_ENABLE | 0xA4 | 0xAEC2 |
| CAM_GET_TWS_INFO | 0xA5 | 0xC002 |
| CAM_SET_TWS_KEY | 0xA6 | 0xC042 |
| CAM_SET_TWS_UI_ENABLE | 0xA7 | 0xC082 |
| CAM_SET_TWS_SOUND_MODE | 0xA8 | 0xC0C2 |
| CAM_SET_ZOOM_PARAM | 0xA9 | 0xC182 |
| CAM_GET_ZOOM_PARAM | 0xAA | 0xC1C2 |
| CAM_NTY_CAMERA_STATUS | 0xAB | 0x1C02 |
| CAM_NTY_CAMERA_EVENT | 0xAC | 0x1C42 |
| CAM_NTY_CAMERA_WARN | 0xAD | 0x1C82 |
| CAM_NTY_CAMERA_ERROR | 0xAE | 0x1CC2 |
| CAM_GET_CAMERA_EVENT | 0xAF | 0x1D02 |
| CAM_SET_SRT_ATTRIBUTE | 0xB0 | 0x1D42 |
| CAM_SET_APP_ATTRIBUTE | 0xB1 | 0x1DC2 |
| CAM_GET_APP_ATTRIBUTE | 0xB2 | 0x1E02 |
| CAM_GET_IMAGE_RANGE | 0xB3 | 0x2F02 |
| CAM_SET_UVC_MODE | 0xB4 | 0xC402 |
| CAM_GET_UVC_MODE | 0xB5 | 0xC442 |
| CAM_GET_ACCESSORY_CAPABILITY | 0xB6 | 0xC802 |
| CAM_GET_ACCESSORY_VERSION | 0xB7 | 0xC842 |
| CAM_GET_ACCESSORY_UPGRADE_INFO | 0xB8 | 0xC882 |

**Ai (receiver 0x04):**

| name | cmdId | wireCmd |
|---|---|---|
| AI_SET_GIM_SPEED | 0x00 | 0x6484 |
| AI_SET_GIM_SPEED_TIME | 0x01 | 0x64C4 |
| AI_SET_GIM_SPEED_EULER | 0x02 | 0x6504 |
| AI_SET_GIM_SPEED_MOTOR | 0x03 | 0x6544 |
| AI_SET_GIM_EULER_DEG | 0x04 | 0x6404 |
| AI_SET_GIM_MOTOR_DEG | 0x05 | 0x6444 |
| AI_NTY_GIM_STATUS | 0x06 | 0x6644 |
| AI_SET_GIM_BOOT_POS | 0x07 | 0x3844 |
| AI_GET_GIM_BOOT_POS | 0x08 | 0x3884 |
| AI_RST_GIM_BOOT_POS | 0x09 | 0x38C4 |
| AI_TRG_GIM_BOOT_POS | 0x0A | 0x3904 |
| AI_GET_GIM_STATE | 0x0B | 0x6604 |
| AI_SET_GIM_STOP | 0x0C | 0x6704 |
| AI_SET_APP_SDK_CFG | 0x0D | null |
| AI_SET_APP_TARGET_NONE | 0x0E | null |
| AI_SET_APP_TARGET_DEFAULT | 0x0F | null |
| AI_SET_GESTURE_SPECIFIED | 0x10 | null |
| AI_SET_TRACK_MODE | 0x11 | 0x0CC4 |
| AI_NTY_STATUS | 0x12 | 0x00C4 |
| AI_GET_QUICK_STATUS | 0x13 | 0x0104 |
| AI_NTY_QUICK_STATUS | 0x14 | 0x0144 |
| AI_SET_BTN_MODE_FOR_ME | 0x15 | null |
| AI_GET_ZONE_TRACK_PRESET_LIST | 0x16 | 0x1344 |
| AI_SET_ZONE_TRACK_PRESET_ADD | 0x17 | 0x1384 |
| AI_SET_ZONE_TRACK_PRESET_DELETE | 0x18 | 0x13C4 |
| AI_SET_ZONE_TRACK_PRESET_UPDATE | 0x19 | 0x1404 |
| AI_GET_ZONE_TRACK_PRESET_ID_VALUE | 0x1A | 0x1444 |
| AI_GET_ZONE_TRACK_PRESET_ID_NAME | 0x1B | 0x1504 |
| AI_SET_ZONE_TRACK_PRESET_ID_NAME | 0x1C | 0x1544 |
| AI_SET_ZONE_TRACK_PRESET_TRIG | 0x1D | 0x1604 |
| AI_SET_ZONE_TRACK_GIM_ENABLED | 0x1E | 0x0844 |
| AI_SET_ZONE_TRACK_PAN_MIN | 0x1F | 0x4C04 |
| AI_GET_ZONE_TRACK_PAN_MIN | 0x20 | 0x4C44 |
| AI_SET_ZONE_TRACK_PAN_MAX | 0x21 | 0x4C84 |
| AI_GET_ZONE_TRACK_PAN_MAX | 0x22 | 0x4CC4 |
| AI_SET_ZONE_TRACK_PITCH_MIN | 0x23 | 0x4D04 |
| AI_GET_ZONE_TRACK_PITCH_MIN | 0x24 | 0x4D44 |
| AI_SET_ZONE_TRACK_PITCH_MAX | 0x25 | 0x4D84 |
| AI_GET_ZONE_TRACK_PITCH_MAX | 0x26 | 0x4DC4 |
| AI_SET_ZONE_TRACK_AUTO_SELECT | 0x27 | 0x4E04 |
| AI_GET_ZONE_TRACK_AUTO_SELECT | 0x28 | 0x4E44 |
| AI_SET_ZONE_TRACK_INIT_POS | 0x29 | 0x4E84 |
| AI_GET_ZONE_TRACK_INIT_POS | 0x2A | 0x4EC4 |
| AI_RST_ZONE_TRACK_INIT_POS | 0x2B | 0x4F04 |
| AI_TRG_ZONE_TRACK_INIT_POS | 0x2C | 0x4F44 |
| AI_RST_ZONE_TRACK_PAN_MIN | 0x2D | 0x4F84 |
| AI_RST_ZONE_TRACK_PAN_MAX | 0x2E | 0x4FC4 |
| AI_RST_ZONE_TRACK_PITCH_MIN | 0x2F | 0x5004 |
| AI_RST_ZONE_TRACK_PITCH_MAX | 0x30 | 0x5044 |
| AI_SET_LIMITED_ZONE_TRACK_ENABLED | 0x31 | 0x5084 |
| AI_GET_LIMITED_ZONE_TRACK_ENABLED | 0x32 | 0x50C4 |
| AI_SET_CAMERA_ZOOM_RATIO | 0x33 | 0x6884 |
| AI_GET_GIMBAL_PRESET_LIST | 0x34 | 0x3B44 |
| AI_GET_GIMBAL_PRESET_LIST2 | 0x35 | 0x3D04 |
| AI_SET_GIMBAL_PRESET_ADD | 0x36 | 0x3944 |
| AI_SET_GIMBAL_PRESET_DELETE | 0x37 | 0x3984 |
| AI_SET_GIMBAL_PRESET_UPDATE | 0x38 | 0x3A04 |
| AI_GET_GIMBAL_PRESET_ID_VALUE | 0x39 | 0x3A44 |
| AI_GET_GIMBAL_PRESET_ID_NAME | 0x3A | 0x3B04 |
| AI_GET_GIMBAL_PRESET_ID_NAME2 | 0x3B | 0x3CC4 |
| AI_SET_GIMBAL_PRESET_ID_NAME | 0x3C | 0x3A84 |
| AI_SET_GIMBAL_PRESET_TRIG | 0x3D | 0x39C4 |
| AI_SET_PRESETS_ACTIONS | 0x3E | 0x3D84 |
| AI_GET_PRESETS_ACTIONS | 0x3F | 0x3DC4 |
| AI_SET_PRESET_UPDATE_ONLY | 0x40 | 0x3E04 |
| AI_SET_BOOT_PRESETS_ACTIONS | 0x41 | 0x3E44 |
| AI_GET_BOOT_PRESETS_ACTIONS | 0x42 | 0x3E84 |
| AI_SET_BOOT_PRESET_UPDATE_ONLY | 0x43 | 0x3EC4 |
| AI_SET_INITIAL_POSITION_OPA | 0x44 | 0x1C84 |
| AI_GET_INITIAL_POSITION_OPA | 0x45 | 0x1C44 |
| AI_GET_PRESET_OPA | 0x46 | 0x1D04 |
| AI_SET_PRESET_OPA | 0x47 | 0x1D44 |
| AI_SET_HAND_TRACK | 0x48 | 0x2244 |
| AI_GET_HAND_TRACK_STATE | 0x49 | 0x2004 |
| AI_SET_HAND_TRACK_INIT_POS | 0x4A | 0x24C4 |
| AI_GET_HAND_TRACK_INIT_POS | 0x4B | 0x2504 |
| AI_RST_HAND_TRACK_INIT_POS | 0x4C | 0x2544 |
| AI_TRG_HAND_TRACK_INIT_POS | 0x4D | 0x2584 |
| AI_SET_HAND_ZONE_PAN_MIN | 0x4E | 0x2044 |
| AI_SET_HAND_ZONE_PAN_MAX | 0x4F | 0x20C4 |
| AI_SET_HAND_ZONE_PITCH_MIN | 0x50 | 0x2144 |
| AI_SET_HAND_ZONE_PITCH_MAX | 0x51 | 0x21C4 |
| AI_RST_HAND_ZONE_PAN_MIN | 0x52 | 0x25C4 |
| AI_RST_HAND_ZONE_PAN_MAX | 0x53 | 0x2604 |
| AI_RST_HAND_ZONE_PITCH_MIN | 0x54 | 0x2644 |
| AI_RST_HAND_ZONE_PITCH_MAX | 0x55 | 0x2684 |
| AI_SET_HAND_TRACK_GIM_ENABLED | 0x56 | 0x26C4 |
| AI_SET_GESTURE_TARGET | 0x57 | 0x30C4 |
| AI_SET_GESTURE_ZOOM | 0x58 | 0x3144 |
| AI_SET_GESTURE_RECORD | 0x59 | 0x31C4 |
| AI_SET_GESTURE_ZOOM_RATIO | 0x5A | 0x3244 |
| AI_SET_GESTURE_DYNAMIC_ZOOM | 0x5B | 0x3344 |
| AI_SET_GESTURE_DIR_MIRROR | 0x5C | 0x33C4 |
| AI_SET_ZONE_TRACK_ENABLE | 0x5D | 0x08C4 |
| AI_SET_AI_AUTO_ZOOM_ENABLE | 0x5E | 0x0A44 |
| AI_SET_AI_ZOOM_SCALE | 0x5F | 0x0AC4 |
| AI_SET_AI_PART_TRACK | 0x60 | 0x1584 |
| AI_SET_AI_ENABLE | 0x61 | 0x0244 |
| AI_SET_AI_WORK_MODE | 0x62 | 0x0284 |
| AI_NTY_UPDATE_POS | 0x63 | 0x3B84 |
| AI_SET_YAW_REVERSE | 0x64 | 0x3B84 |
| AI_SET_YAW_REVERSE2 | 0x65 | 0x3C04 |
| AI_SET_TRACK_SPEED | 0x66 | 0x0944 |
| AI_NTY_ALL_CTRL_PARAM | 0x67 | 0x0404 |
| AI_SET_TARGET_BY_POS | 0x68 | 0x0404 |
| AI_SET_TARGET_BY_BOX | 0x69 | 0x0444 |
| AI_SET_NO_TRACK | 0x6A | 0x0444 |
| AI_SET_BIGGEST_TARGET | 0x6B | 0x0484 |
| AI_SET_CENTER_TARGET | 0x6C | 0x04C4 |
| AI_SET_CENTER_POS | 0x6D | 0x0544 |
| AI_SET_CANCEL_TARGET | 0x6E | 0x0504 |
| AI_SET_AI_TRACK_MODE | 0x6F | 0x0584 |
| AI_SET_AUTO_GROUP | 0x70 | 0x0604 |
| AI_CANCEL_AUTO_GROUP | 0x71 | 0x0644 |
| AI_NTY_DELETE_POS | 0x72 | 0x3BC4 |
| AI_NTY_ATTITUDE_CHANGED | 0x73 | 0x3F04 |
| AI_SET_AUTO_OFFSET | 0x74 | 0x5204 |
| AI_GET_AUTO_OFFSET_ENABLE | 0x75 | 0x5244 |
| AI_GET_HORIZONTAL_OFFSET | 0x76 | 0x0C04 |
| AI_SET_HORIZONTAL_OFFSET | 0x77 | 0x0BC4 |
| AI_GET_VERTICAL_OFFSET | 0x78 | 0x0C84 |
| AI_SET_VERTICAL_OFFSET | 0x79 | 0x0C44 |
| AI_SET_GESTURE_TRACK_PARAMETER | 0x7A | 0x2044 |
| AI_GET_GESTURE_TRACK_PARAMETER | 0x7B | 0x2084 |
| AI_SET_GESTURE_PARAMETER | 0x7C | 0x3444 |
| AI_GET_GESTURE_PARAMETER | 0x7D | 0x3484 |
| AI_SET_GIMBAL_PARAMETER | 0x7E | 0x3F44 |
| AI_GET_GIMBAL_PARAMETER | 0x7F | 0x3F84 |
| AI_SELECT_TARGET | 0x80 | 0x0684 |
| AI_SET_TARGET_ZOOM_TYPE | 0x81 | 0x06C4 |
| AI_SET_TARGET_VIEW_TYPE | 0x82 | 0x0704 |
| AI_SET_CONTROL_PARAMETER | 0x83 | 0x5444 |
| AI_GET_CONTROL_PARAMETER | 0x84 | 0x5484 |

> **Collision note (reproduce verbatim):** several Ai names share a wireCmd — `AI_NTY_UPDATE_POS` and `AI_SET_YAW_REVERSE` both `0x3B84`; `AI_NTY_ALL_CTRL_PARAM` and `AI_SET_TARGET_BY_POS` both `0x0404`; `AI_SET_TARGET_BY_BOX` and `AI_SET_NO_TRACK` both `0x0444`; `AI_SET_GESTURE_TRACK_PARAMETER` and `AI_SET_HAND_ZONE_PAN_MIN` both `0x2044`. Encoders address by NAME via `OP_BY_NAME`; the reverse map (wireCmd→name) is therefore ambiguous. Also note `CAM_SET_DEV_STATUS` and the destructive `CAM_SET_POWER_CTRL` share wireCmd `0xA0C2`.

**Gimbal (receiver 0x03):**

| name | cmdId | wireCmd |
|---|---|---|
| GIM_GET_STATE | 0x00 | 0x0043 |
| GIM_SET_MOTOR | 0x01 | 0x00C3 |
| GIM_SET_SPEED | 0x02 | 0x0103 |
| GIM_SET_SPEED_ANGLE | 0x03 | 0x0183 |
| GIM_SET_ANGULAR_VELOCITY_POSITION | 0x04 | 0x0283 |
| GIM_SET_LOCK | 0x05 | null |
| GIM_SET_RESET | 0x06 | null |
| GIM_SET_SMOOTH | 0x07 | 0x0503 |
| GIM_GET_INFO | 0x08 | 0x1303 |

**SysMg (receiver 0x12):** SYS_MG_SET_WIFI_MODE 0x104D, SYS_MG_GET_WIFI_MODE 0x108D, SYS_MG_SET_WIFI_COUNTRY_CODE 0x10CD, SYS_MG_TRG_WIFI_SCAN 0x114D, SYS_MG_GET_WIFI_SCAN 0x118D, SYS_MG_NTY_STA_STATUS 0x140D, SYS_MG_GET_STA_STATUS 0x144D, SYS_MG_GET_WIFI_STA_CFG 0x154D, SYS_MG_REMOVE_STA_BSS 0x15CD, SYS_MG_SET_WIFI_CONN 0x160D, SYS_MG_SET_WIFI_STA_ARQ 0x16CD, SYS_MG_NTY_WIFI_AP_STATUS 0x180D, SYS_MG_GET_WIFI_AP_STATUS 0x184D, SYS_MG_GET_NETWORK_NIC_MAC 0x610D, SYS_MG_SET_WIFI_IFACE 0x170D, SYS_MG_GET_WIFI_STA_CFG_NEW 0x174D, SYS_MG_RESTART_WIFI_STA 0x150D, SYS_MG_SET_INDICATOR_STATE 0x700D, SYS_MG_CLEAR_INDICATOR_STATE 0x704D, SYS_MG_SET_BUZZER_ENABLED 0x728D, SYS_MG_SET_BUZZER_DISABLED 0x72CD, SYS_MG_GET_BUZZER_STATUS 0x730D, SYS_MG_SET_LED_BRIGHTNESS 0x750D, SYS_MG_GET_LED_BRIGHTNESS 0x754D, SYS_MG_GET_LED_ENABLE 0x758D, SYS_MG_SET_LED_ENABLE 0x75CD, SYS_MG_SET_MDNS_HOST_NAME 0x040D, SYS_MG_SET_DEVICE_NAME 0x044D, SYS_MG_GET_DEVICE_NAME 0x048D, SYS_MG_RESET_CONNECTION 0x018D, SYS_MG_GET_STATUS 0x014D, SYS_MG_GET_ETHERNET_STATUS 0x308D, SYS_MG_SET_ETHERNET_CONFIG 0x314D, SYS_MG_GET_ETHERNET_CONFIG 0x318D, SYS_MG_RESTART_ETHERNET 0x31CD, SYS_MG_TRG_WIFI_SCAN_ASYNC 0x11CD, SYS_MG_GET_WIFI_SCAN_STATUS 0x120D.

**TXBle (receiver 0x18):** PRI_BLE_TX_GET_DEV_MAC 0x0053, PRI_BLE_TX_GET_VERSION 0x0153, PRI_BLE_TX_SET_PAIR_ENABLE 0x0C13, PRI_BLE_TX_SET_PAIR_DISABLE 0x0C53, PRI_BLE_TX_SET_DEV_NAME 0x0DD3, PRI_BLE_TX_GET_DEV_NAME 0x0E13, PRI_BLE_TX_CLEAR_PAIR_INFO 0x0E93, PRI_BLE_TX_GET_UPGRADE 0x1413, PRI_BLE_TX_SET_VIB_ENABLE 0x2153, PRI_BLE_TX_SET_KEY_MODE 0x2413, PRI_BLE_TX_GET_KEY_MODE 0x2453, PRI_BLE_TX_SET_LED_MODE 0x2493, PRI_BLE_TX_GET_LED_MODE 0x24D3, PRI_BLE_TX_SET_KEY_ENABLE 0x2513, PRI_BLE_TX_SET_LED_ENABLE 0x2553, PRI_BLE_TX_GET_KEY_ENABLE 0x2593, PRI_BLE_TX_GET_LED_ENABLE 0x25D3, PRI_BLE_TX_SET_AUDIO_EQ_MODE 0x2C13, PRI_BLE_TX_GET_AUDIO_EQ_MODE 0x2C53, PRI_BLE_TX_SET_AUDIO_NR_LEVEL 0x2C93, PRI_BLE_TX_GET_AUDIO_NR_LEVEL 0x2CD3, PRI_BLE_TX_SET_AUDIO_NR_ENABLE 0x2D13, PRI_BLE_TX_SET_AUDIO_GAIN 0x2D53, PRI_BLE_TX_GET_AUDIO_GAIN 0x2D93, PRI_BLE_TX_SET_AUDIO_MUTE_ENABLE 0x2DD3, PRI_BLE_TX_GET_AUDIO_MUTE_ENABLE 0x2E13, PRI_BLE_TX_GET_AUDIO_NR_ENABLE 0x2E53, PRI_BLE_TX_GET_BATT_LEVEL 0x3013, PRI_BLE_TX_GET_BATT_CHARGING_STATUS 0x3053, PRI_BLE_TX_SET_AUTO_SHUTDOWN 0x3113.

**PrimaryBle (receiver 0x13):** PRI_BLE_GET_MAC 0x004E, PRI_BLE_GET_VER 0x014E, PRI_BLE_WAKE_HOST_UP 0x01CE, PRI_BLE_GET_ADV_DATA 0x040E, PRI_BLE_GET_SCAN_RSP_DATA 0x044E, PRI_BLE_GET_STATUS 0x048E, PRI_BLE_SET_DISCONN_NOTIFY 0x080E, PRI_BLE_GET_UG_STATUS 0x140E, PRI_BLE_PAIRING_ENABLED 0x0C0E, PRI_BLE_PAIRING_DISABLED 0x0C4E, PRI_BLE_PAIRING_EXIT 0x0DCE, PRI_BLE_SWITCH_APP_MODE 0x204E, PRI_BLE_SET_START_INQUIRY 0x288E, PRI_BLE_STOP_INQUIRY 0x28CE, PRI_BLE_GET_INQUIRED_DEVICE_LIST 0x290E, PRI_BLE_CONNECT_DEVICE 0x294E, PRI_BLE_DISCONNECT_DEVICE 0x298E, PRI_BLE_GET_LAST_CONNECTED_DEVICE 0x2B0E.

**RemoteBle (receiver 0x14):** PRI_BLE_REMOTE_GET_STATUS 0x0C8F.

**Upgrade (receiver 0x0D), non-destructive:** UG_GET_RESULT 0x0088, UG_GET_STATE 0x00C8, UG_GET_PKG_VER 0x0408, UG_GET_UUID 0x1808, UG_GET_DEV_INFO 0x1948, UG_GET_SN 0x18C8, UG_GET_UG_RESULT 0x0088, UG_GET_UG_VER 0x1A48, UG_SET_PACK_LOG 0x1A88, UG_WAKE_HOST_UP 0x1B48, UG_SET_HDMI_TIMING 0x1E08, UG_GET_HDMI_TIMING 0x1E48.

**Route (receiver 0x01):** ROUTE_SET_REGISTER 0x0801, ROUTE_NTY_DISCONNECT 0x0841, ROUTE_SET_CLIENT_ALIVE 0x0881, ROUTE_NTY_SERVER_ALIVE 0x08C1, ROUTE_SET_TOKEN 0x0901, ROUTE_GET_TOKEN 0x0941, ROUTE_NTY_CONN_EVENT 0x09C1, ROUTE_UN_REGISTER 0x0B01.

### 3b. DESTRUCTIVE commands (gate behind `isDestructive(name)`)

These can brick/erase/flash/reset/cut power/run arbitrary code. Exported as `DESTRUCTIVE_NAMES` (a Set), `DESTRUCTIVE` (rows), `isDestructive(name)`.

| name | subsystem | cmdId | wireCmd | receiver |
|---|---|---|---|---|
| CAM_SET_SYSCALL | Camera | 0x00 | 0xAA42 | 0x02 |
| CAM_SET_FACTORY_RESET | Camera | 0x01 | 0xA802 | 0x02 |
| CAM_SET_MODULE_ACTIVATE | Camera | 0x21 | 0x0E02 | 0x02 |
| CAM_SET_SD_FORMAT | Camera | 0x85 | 0x6442 | 0x02 |
| CAM_SET_POWER_CTRL | Camera | 0x8B | 0xA0C2 | 0x02 |
| UG_SET_EVENT | Upgrade | 0x00 | 0x0048 | 0x0D |
| UG_SET_UPGRADE | Upgrade | 0x05 | 0x1848 | 0x0D |
| UG_SET_IP | Upgrade | 0x06 | 0x1908 | 0x0D |
| UG_SET_UG_MODE | Upgrade | 0x09 | 0x1988 | 0x0D |
| UG_SET_RUN_CMD | Upgrade | 0x0D | 0x1AC8 | 0x0D |
| UG_SET_MTP_STORAGE | Upgrade | 0x0E | 0x1B08 | 0x0D |
| UG_SWITCH_USB_MODE | Upgrade | 0x10 | 0x1B88 | 0x0D |
| UG_SET_ACCESSORY_UPGRADE | Upgrade | 0x13 | 0x2008 | 0x0D |
| FTY_SET_FTP | Factory | 0x00 | 0x1989 | 0x0E |
| FTY_SET_COPY_FILE | Factory | 0x01 | 0x19C9 | 0x0E |
| FTY_FILE_TRANSFER | Factory | 0x02 | 0x3009 | 0x0E |

---

## 3c. Command Payload Encoders (V3 frames)

Encoding primitives (`encoding.js`), all little-endian: `f32le(n)` = 4-byte IEEE-754 float LE; `u16le(n)` = 2-byte `(n & 0xFFFF)`; `u32le(n)` = 4-byte `(n >>> 0)`; `i32le(n)` = 4-byte `(n | 0)` signed. `concat(...)` = byte concatenation.

Two frame flavours produced by `vendorOp`:
- **SET / default:** `flags` omitted → frame uses `0x25`, with nested payload.
- **`encodeVendorGet(name)`:** empty payload, `flags = 0x01` (header-only). Required for the device to answer GETs.
- `encodeVendorProbe(name, payload)`: arbitrary payload, default flags (for RE/diagnostics).

| Function | Opcode (wireCmd) | Payload layout | Notes / ranges / units |
|---|---|---|---|
| `encodeSetRunStatus(state)` | CAM_SET_DEV_STATUS (0xA0C2) | 4 bytes: `[wakeByte, 0, 0, 0]` where wakeByte = `state==="run"?0:1` | **0 = wake, 1 = sleep** |
| `encodePtzMoveAngle(yaw,pitch,roll)` | AI_SET_GIM_MOTOR_DEG (0x6444) | 12 bytes: `f32le(roll) ++ f32le(pitch) ++ f32le(yaw)` | **Wire order is [roll, pitch, yaw]** — data[0:4]=roll, [4:8]=pitch, [8:12]=yaw. Roll unused on Tiny 2. Degrees. Logical arg order (yaw,pitch,roll) is reversed at encode time. |
| `encodePtzMoveSpeed(yaw,pitch,roll)` | AI_SET_GIM_SPEED (0x6484) | 12 bytes: same [roll,pitch,yaw] float order | speed units |
| `encodeRecenter()` | GIM_SET_MOTOR (0x00C3) | 6 zero bytes | receiver 0x03 (Gimbal) |
| `encodeAiTrackEnable(mode)` | AI_SET_AI_TRACK_MODE (0x0584) | 8 bytes: `u32le(subject) ++ u32le(view)` | See AI_TRACK_VIEW table below |
| `encodeAiTrackDisable()` | AI_SET_CANCEL_TARGET (0x0504) | empty | |
| `encodeAiGroupEnable()` | AI_SET_AUTO_GROUP (0x0604) | 8 zero bytes | |
| `encodeAiGroupDisable()` | AI_CANCEL_AUTO_GROUP (0x0644) | empty | |
| `encodeAiTrackSpeed(speed)` | AI_SET_TRACK_MODE (0x0CC4) | 1 byte: `[AI_TRACK_SPEED[speed]]` | standard=0, sport=2 |
| `encodeZoomWithSpeed(ratioX100, speed)` | CAM_SET_ZOOM_ABSOLUTE (0x1942) | 8 bytes: `u32le(speed) ++ u32le(ratioX100)` | **speed FIRST, ratio SECOND**. ratio is ratio×100 (1.5× → 150). |
| `encodeFaceFocus(enable)` | CAM_SET_FACE_FOCUS (0x3602) | 4 bytes: `i32le(enable?1:0)` | face-priority autofocus |
| `encodeSetExposure(manual, raw)` | CAM_SET_EXPOSURE_TINY2 (0x2982) | **5 bytes**: `[manual?1:0] ++ u32le(raw)` | **Width is load-bearing: a 4-byte payload is SILENTLY DISCARDED.** `raw` range 0..65535. Sets mode+value together. |
| `encodeGetExposureMode()` | CAM_GET_EXPOSURE_MODE (0x2402) | empty | |
| `encodeGetExposureValue()` | CAM_GET_EXPOSURE_TINY2 (0x2942) | empty | |
| `encodeGetExposureRange()` | CAM_GET_EXPOSURE_RANGE_TINY2 (0x29C2) | empty | |
| `encodeGetFaceFocus()` | CAM_GET_FACE_FOCUS (0x35C2) | empty | |

**Hardware quirks reproduced from source:**
- **Exposure mode:** the separate `CAM_SET_EXPOSURE_MODE` (0x2442) command is **inert** (writing 0 then 1 left mode pinned); mode is only settable through `CAM_SET_EXPOSURE_TINY2`. Mode byte semantics **UNVERIFIED**: writing 0 reads back as 2, writing 1 reads back as 1 — so the readback encoding is 1/2 not 0/1. Encoder maps `manual→1`, `auto→0` on the write side.
- **Exposure path:** the UVC/V4L2 exposure control is a stub (VIDIOC_S_CTRL accepted but ignored) — must use V3 frames.
- **Gimbal move order:** sending yaw into the roll slot made move-to-angle appear inert; correct wire order is [roll, pitch, yaw].

### Decoders (V3 reply payloads)

| Function | Input | Output |
|---|---|---|
| `decodeSerial(payload)` | UG_GET_SN reply, 14 ASCII bytes | ASCII string; everything from first NUL onward dropped, then `.trim()` |
| `decodeExposureRange(payload)` | ≥8 bytes | `{ min: i32le@0, max: i32le@4 }`; throws if <8 bytes |
| `decodeFaceFocus(payload)` | ≥4 bytes | `{ enabled: (i32le@0) !== 0 }`; throws if <4 bytes |

### AI enums (from `commands.js`)

**AI_TRACK_VIEW** — `[subject, view]` for `encodeAiTrackEnable`, each field a u32le:

| mode | subject | view |
|---|---|---|
| human-normal | 0 | 0 |
| human-full-body | 0 | 4 |
| human-half-body | 0 | 3 |
| human-close-up | 0 | 2 |
| human-auto-view | 0 | 1 |
| animal-normal | 1 | 0 |
| animal-close-up | 1 | 2 |
| animal-auto-view | 1 | 1 |

**AI_TRACK_SPEED:** `standard=0` (slower follow), `sport=2` (snappier). Exposed as `AI_TRACK_SPEEDS`.

---

## 4. Status Block Decode — `decodeStatus(block)`

**Transport:** UVC XU selector 6, GET_CUR — a flat fixed-offset snapshot, **NOT a V3 frame** (no magic, no CRC). Offsets confirmed against OpenFoxes/Tiny4Linux `status.rs`.

**Minimum size:** the decoder throws `status block too short` if `block.length <= 0x24` (i.e. it needs at least 0x25 = 37 bytes to reach the track-speed byte). The buffer read is the standard 60-byte XU buffer.

**Hardware quirk (reproduce):** a **sleeping camera serves a stale block** — read zoom/FOV fields only after the readiness gate.

| Offset | Const name | Width | Field | Conversion / meaning |
|---|---|---|---|---|
| 0x02 | STATUS_OFF_SLEEP | 1 | awake | `block[0x02] === 0` → awake (0 = awake, 1 = sleep) |
| 0x04 | STATUS_OFF_ZOOM_PCT | 1 | zoomPercent | raw byte, 0–100 over the UVC 1.0–2.0 zoom range |
| 0x06 | STATUS_OFF_HDR | 1 | hdr | `block[0x06] !== 0` (0 = off, non-zero = on) |
| 0x07 | STATUS_OFF_FACE_AE | 1 | faceAe | `block[0x07] === 1` (1 = face-priority AE, 0 = global AE). HW-verified 2026-07-18. |
| 0x11 | STATUS_OFF_FOV_MODE | 1 | fovMode | FOV_MODE_TABLE lookup (same enum as FOV_VALUE); 3 = vendor "FovTypeNull"/custom (a continuous zoom overrode the discrete modes) |
| 0x18 | STATUS_OFF_AI_MODE_M | 1 | aiMode (m) | first value of AI mode tuple |
| 0x1C | STATUS_OFF_AI_MODE_N | 1 | aiMode (n) | second value of AI mode tuple → AI_MODE_TABLE[`"m,n"`] |
| 0x24 | STATUS_OFF_TRACK_SPEED | 1 | trackSpeed | TRACK_SPEED_TABLE lookup |

**Output object:**
```
{
  awake:       block[0x02] === 0,
  hdr:         block[0x06] !== 0,
  faceAe:      block[0x07] === 1,
  aiMode:      AI_MODE_TABLE["m,n"]      ?? "unknown",   // m=block[0x18], n=block[0x1C]
  trackSpeed:  TRACK_SPEED_TABLE[block[0x24]] ?? "unknown",
  fovMode:     FOV_MODE_TABLE[block[0x11]]     ?? "unknown",
  zoomPercent: block[0x04],
}
```

**AI_MODE_TABLE** (`"m,n"` → mode):

| key "m,n" | mode |
|---|---|
| "0,0" | no-tracking |
| "2,0" | normal |
| "2,1" | upper-body |
| "2,2" | close-up |
| "2,3" | headless |
| "2,4" | lower-body |
| "5,0" | desk |
| "4,0" | whiteboard |
| "3,0" | hand |
| "1,0" | group |

**Hardware quirk (reproduce verbatim):** Hand shows as m=3 on live Tiny 2 firmware (verified 2026-07-18); the Tiny4Linux reference lists m=6. **Do NOT map "6,0" to "hand"** — on this firmware m=6 is the MID-SWITCH TRANSIENT the device parks at while changing framing, and it must fall through to `"unknown"` so framing-verification keeps polling. Wire evidence (XU sel 6 @ 60 ms, normal→upper-body): m=2,n=0 → m=6,n=0 (~200 ms transient) → m=2,n=1 (landed).

**Track-speed offset quirk:** track speed is at **0x24** on the Tiny 2, NOT the reference's 0x21 (which reads a constant here). Confirmed 2026-07-13 against OBSBOT Center Standard/Sport.

**TRACK_SPEED_TABLE:** `0 → standard`, `2 → sport`.
**FOV_MODE_TABLE:** `0 → wide`, `1 → medium`, `2 → narrow`, `3 → custom`.

---

## 5. Preset Encoding — `preset.js`

**Device has 3 preset slots.** Slots are 1-based in the API; the wire index is 0-based: `idx(slot) = u32le(slot - 1)`. Receiver is always `0x04` (Ai). All these use `buildFrame` directly (default `flags = 0x25`).

**Command wireCmds resolved by name:**

| CMD alias | opcode name | wireCmd |
|---|---|---|
| ADD | AI_SET_GIMBAL_PRESET_ADD | 0x3944 |
| UPDATE | AI_SET_PRESET_UPDATE_ONLY | 0x3E04 |
| RECALL | AI_SET_GIMBAL_PRESET_TRIG | 0x39C4 |
| DELETE | AI_SET_GIMBAL_PRESET_DELETE | 0x3984 |
| SET_NAME | AI_SET_GIMBAL_PRESET_ID_NAME | 0x3A84 |
| BOOT_POSE (legacy) | AI_SET_BOOT_PRESET_UPDATE_ONLY | 0x3EC4 |
| BOOT_FLAGS (legacy) | AI_SET_BOOT_PRESETS_ACTIONS | 0x3E44 |
| GIM_BOOT_POS_SET | AI_SET_GIM_BOOT_POS | 0x3844 |
| GIM_BOOT_POS_RESET | AI_RST_GIM_BOOT_POS | 0x38C4 |
| GIM_BOOT_POS_TRIGGER | AI_TRG_GIM_BOOT_POS | 0x3904 |

**Pose type:** `{ pan, tilt, roll, zoom }` (all numbers). Note a **field-name swap** between wire and pose: on the wire the first float slot is `pan` (from pose.pan), second is `tilt`; but the entry *decoder* (§5.6) reads pitch→tilt, yaw→pan.

**`poseBytes(p)`** (used by ADD and UPDATE) = 20 bytes:
```
f32le(p.pan) ++ f32le(p.tilt) ++ f32le(p.roll) ++ f32le(p.zoom) ++ f32le(-1000)
```
The trailing `-1000.0` is a **sentinel** (golden-tests pinned).

### 5.1 Encoders

| Function | wireCmd | Payload |
|---|---|---|
| `encodePresetAdd(seq, slot, pose)` | 0x3944 | `idx(slot) ++ poseBytes(pose)` (4 + 20 = 24 bytes) |
| `encodePresetUpdate(seq, slot, pose)` | 0x3E04 | `idx(slot) ++ poseBytes(pose)` (24 bytes) |
| `encodePresetRecall(seq, slot)` | 0x39C4 | `idx(slot) ++ f32le(1) ++ f32le(1) ++ f32le(1) ++ f32le(1)` (4 + 16 = 20 bytes) |
| `encodePresetDelete(seq, slot)` | 0x3984 | `idx(slot)` (4 bytes) |
| `encodePresetSetName(seq, slot, name)` | 0x3A84 | `idx(slot) ++ Buffer.from(name, "ascii")` |

### 5.2 Legacy "As Initial State" replay (retained, not tool-reachable)

- `encodeBootPose(seq, slot, pose)` → cmd 0x3EC4 (AI_SET_BOOT_PRESET_UPDATE_ONLY): payload `idx(slot) ++ f32le(pose.pan) ++ f32le(pose.tilt) ++ f32le(pose.roll) ++ f32le(pose.zoom) ++ f32le(0)`. **Unlike ADD/UPDATE the trailing float is plain 0.0, NOT the −1000 sentinel** (captured from real OBSBOT Center wire frame; do not fold into `poseBytes`).
- `encodeBootFlags(seq)` → cmd 0x3E44 (AI_SET_BOOT_PRESETS_ACTIONS): payload is a captured 40-byte block, **internal structure NOT decoded**, carries no slot binding (target slot comes from the preceding `encodeBootPose` frame). Hex constant:
  ```
  feffffffffffffff80ffffffffffffff00000000ffffffff00000000000000000000000000000000
  ```

### 5.3 Boot-pose family (direct, reversible)

- `encodeGimBootPosSet(seq, pose)` → cmd 0x3844: **payload width is load-bearing — device requires 24 bytes (six float32) and SILENTLY DISCARDS a 20-byte payload.** Layout:
  ```
  u32le(0) ++ f32le(pose.pan) ++ f32le(pose.tilt) ++ f32le(pose.roll) ++ f32le(pose.zoom) ++ f32le(0)
  ```
  Slot field is 0 (boot pose is a single global setting, not one of the 3 slots; `u32le(0)` is bit-identical to float32 0.0). **The trailing float32 0.0 is REQUIRED — without it the write is discarded.** Field order from libdev `aiSetGimbalBootPosR`: buf+0x05=yaw([rbx+0xC]), +0x09=pitch([rbx+8]), +0x0D=roll([rbx+4]), +0x11=zoom. Confirmed by readback (AI_GET_GIM_BOOT_POS 0x3884 returns this exact record). Verified: writing `[0,-35,-20,0,1,0]` brought camera up at yaw −34 / pitch −20.
- `encodeGimBootPosReset(seq)` → cmd 0x38C4: empty payload. Restores factory default.
- `encodeGimBootPosTrigger(seq)` → cmd 0x3904: empty payload.

**GET note (reproduce):** vendor GET replies ARE readable once framed with `frame[1] = 0x01` (header-only GET flavour). The earlier belief that the transport couldn't read vendor GETs was wrong — GETs had been sent with the SET flags byte, which the device does not answer.

### 5.4 Preset-list decode — `decodePresetList(block)`

```
count = block[0]
slots = [block[1], block[2], … block[count]]   // count bytes after the count byte
return { count, slots }
```
Transport = UVC XU **selector 12**.

### 5.5 Preset-list validation — `implausiblePresetListReason(block)`

Returns a human-readable reason string, or `null` if safe to trust/echo. Checks in order:
1. `block.length < 1` → `"empty response (0 bytes)"`
2. `count = block[0]; count > 3` → `"implausible slot count {count} (device has 3 slots)"`
3. `block.length < 1 + count` → `"short response: {len} bytes for count={count}"`
4. `block.equals(all-zero of same length)` → `"all-zero response — looks like a failed/short read"` (checks the WHOLE block, not just leading bytes)
5. else `null`

Selector-12 quirk: only echo back exactly what the device just returned (its write semantics for anything else are undecoded); never echo a failed/short/garbage read.

### 5.6 Preset-entry decode — `decodePresetEntry(block)`

Transport = UVC XU **selector 13** (walked). Constants: `ENTRY_END = 0x02`, `ENTRY_HEADER_LEN = 10`.

Fixed header layout (offsets within the entry block):

| Offset | Width | Field | Conversion |
|---|---|---|---|
| 0 | 1 | status | `0x02` = end-of-list marker; all-zero header = failed read / invalid |
| 1 | 1 | slotIdx | 0-based; valid 0..2; `slot = slotIdx + 1` |
| 2–3 | 2 | reserved | — |
| 4–5 | 2 | pitch | `readInt16LE(4) / 100` (signed i16le, scale ÷100, degrees) → pose.tilt |
| 6–7 | 2 | yaw | `readInt16LE(6) / 100` (signed i16le, scale ÷100, degrees) → pose.pan |
| 8 | 1 | zoom | `block[8] / 100` (a committed preset's zoom is always ≥1.0, i.e. block[8] ≥ 100) |
| 9 | 1 | (part of header, unused) | — |
| 10 … NUL | var | name | base64 ASCII from offset 10 up to first NUL (`indexOf(0, 10)`; if none, to end), then base64-decoded to ASCII |

Decode logic and guards (order matters):
1. `block[0] === 0x02` → `{ end: true }`.
2. `block.length < 10` → `{ end: true }` (too short for header; avoids raw RangeError).
3. First 10 bytes all zero → `{ end: true }` (failed/short USB read decodes as status 0x00 / slotIdx 0 / all-zero; never a genuine occupied slot).
4. `slotIdx > 2` → `{ end: true }` (only 0..2 valid on 3-slot device).
5. Otherwise → `{ end: false, slot, name, pose: { pan: yaw, tilt: pitch, roll: 0, zoom } }`.

**Design note (reproduce):** the dangerous failure mode for a create-once resource is a false OCCUPIED read, never a false EMPTY — hence all ambiguous/short/garbage reads collapse to end-of-list.

### 5.7 Slot assembly — `assemblePresetSlots(perSlot)`

Maps a list of decoded entries into the fixed 3-slot view. Builds `Map(slot → entry)`, then for slots `[1,2,3]`:
- present → `{ slot, occupied: true, name, pose }`
- absent → `{ slot, occupied: false, name: null, pose: null }`

---

## 6. UVC Extension-Unit Controls (selector 6, NOT a V3 frame)

`UVC_XU_SELECTOR = 6`. FOV, WDR/HDR, face-AE, and AI-mode writes use a **fixed 60-byte buffer** written via `uvc_xu_set(selector = 6, buf, 0x3c)` (0x3c = 60).

**`uvcExt(tag, value)`** — 60-byte buffer, first three bytes `[tag, 0x01, value & 0xFF]`, rest zero:

| Function | tag | byte[1] | byte[2] | Meaning |
|---|---|---|---|---|
| `encodeFov(fov)` | 0x04 | 0x01 | FOV_VALUE[fov] | wide=0, medium=1, narrow=2 |
| `encodeFaceAe(face)` | 0x03 | 0x01 | face?1:0 | 1=face-priority AE, 0=global AE. Moves status offset 0x07. Precondition: auto-exposure must be ON first. HW-verified 2026-07-18. This is AE priority, distinct from face_focus (autofocus). |
| `encodeHdr(on)` | 0x01 | 0x01 | on?1:0 | HDR/WDR on/off |

`FOV_VALUE = { wide:0, medium:1, narrow:2 }`; exposed as `FOV_TYPES`.

**`encodeAiMode(work, framing="normal")`** — separate 60-byte selector-6 buffer with a different header:
```
b[0] = 0x16
b[1] = 0x02
b[2] = AI_WORK_MODE[work]
b[3] = (work === "human") ? AI_FRAMING[framing] : 0x00
rest = 0
```
`encodeAiTracking(on, mode="normal")` = thin wrapper: `encodeAiMode(on ? "human" : "none", mode)`.

**AI_FRAMING** (`AI_FRAMING_MODES`): normal=0, upper-body=1, close-up=2, headless=3, lower-body=4.
**AI_WORK_MODE** (`AI_WORK_MODES`): none=0, group=1, human=2, hand=3, whiteboard=4, desk=5.
**AI_SCENE_MODES** = `["group", "whiteboard", "desk", "hand"]`.

---

## 7. UVC Standard Controls (IAMCameraControl / IAMVideoProcAmp)

Property IDs and auto/manual flag values (constants from `commands.js`):

| Const | Value | Meaning |
|---|---|---|
| CAMERA_CONTROL_PAN | 0 | CameraControl_Pan |
| CAMERA_CONTROL_TILT | 1 | CameraControl_Tilt |
| CAMERA_CONTROL_FOCUS | 6 | CameraControl_Focus |
| CAMERA_CONTROL_EXPOSURE | 4 | CameraControl_Exposure |
| VIDEO_PROCAMP_WHITE_BALANCE | 7 | VideoProcAmp_WhiteBalance |
| UVC_FLAG_AUTO | 1 | *_Flags_Auto |
| UVC_FLAG_MANUAL | 2 | *_Flags_Manual |

**IMAGE_CONTROL_PROP** (`IMAGE_CONTROLS`): brightness=0, contrast=1, hue=2, saturation=3, sharpness=4, backlight-compensation=8, gain=9.

### Scale/range helpers

- `zoomRatioToUnits(ratio, min, max)` = `Math.round(min + (max - min) * (ratio - 1.0) + 0.001)` — maps a zoom ratio (1.0 = min) to device units; the `+0.001` biases rounding.
- `percentToRange(pct, min, max)` = `Math.round(min + (max - min) * (pct / 100))` — maps 0..100 % onto inclusive [min,max] device units.
- `rangeToPercent(value, min, max)` = `max === min ? 0 : Math.round(((value - min) / (max - min)) * 100)` — inverse; a degenerate range (min===max) yields 0 (no divide-by-zero).

---

## 8. types.js

`types.js` compiles to `export {};` only — it is a **type-only module** (TypeScript interfaces/type aliases erased at build). No runtime values, enums, or constants. All wire-relevant enums live in `commands.js`, `opcodes.js`, and `preset.js` as documented above.

---

## 9. Transport summary (which channel each thing uses)

| Concern | Transport | Framed? | CRC? |
|---|---|---|---|
| Vendor SET/GET commands (§3) | V3 vendor channel, 60-byte frame | Yes | Yes (header + payload) |
| Presets add/update/recall/delete/name, boot-pose (§5.1–5.3) | V3 vendor frame (receiver 0x04) | Yes | Yes |
| Preset list read (§5.4) | UVC XU **selector 12** | No | No |
| Preset entry walk (§5.6) | UVC XU **selector 13** | No | No |
| FOV / HDR / face-AE / AI-mode writes (§6) | UVC XU **selector 6**, 60-byte buffer, len 0x3C | No | No |
| Status snapshot read (§4) | UVC XU **selector 6**, GET_CUR | No | No |
| Pan/tilt/focus/exposure/white-balance/image props (§7) | UVC standard IAMCameraControl / IAMVideoProcAmp | No | No |

**Source file locations:** `/home/millerah/obsbotmcp/node_modules/obsbot-mcp/dist/codec/{frame,crc,encoding,opcodes,commands,preset,types}.js`