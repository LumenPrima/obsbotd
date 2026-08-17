> **Provenance**: This document was extracted from the source of
> [obsbot-mcp](https://www.npmjs.com/package/obsbot-mcp) v0.6.3 (MIT License,
> Copyright (c) 2026 Michael Jordan) as the reimplementation reference for
> obsbotd. Protocol constants, hardware measurements, and quirk notes originate
> from that project's reverse-engineering work.

# obsbot-mcp Tool-Surface Specification (extracted from `dist/mcp/tools.js`, `dist/mcp/render.js`, `dist/mcp/ready.js`, `dist/mcp/framing.js`, `dist/geometry/aim.js`, `README.md`)

Target hardware: **OBSBOT Tiny 2** (USB vid = Remo, pid `0x3564`/`0xFEF8`). 36 tools as of v0.4.0 (all renamed in that release; no backward-compatible aliases).

---

## 0. Shared plumbing, constants, and helpers

### 0.1 Input coercion (all schemas)

Some MCP clients serialize numbers/booleans as strings. Every numeric/boolean param is defensively coerced:

- `num()`: if value is a string, non-empty after trim, and `Number(v)` is not NaN → convert to number; otherwise pass through to number validation.
- `bool()`: string `"true"` → `true`, string `"false"` → `false`, otherwise pass through to boolean validation. **Explicitly not** a generic truthiness coercion — a naive coercion maps any non-empty string (including `"false"`) to `true`, which would be a bug.

### 0.2 `camera` selector

Every camera-addressing tool takes optional `camera: string` (the camera's **serial number**). Resolution semantics:

- omitted + exactly one camera attached → that camera (matches pre-v0.4.0 single-camera behavior).
- omitted + several attached → `AmbiguousCameraError` (error names every attached serial).
- unknown serial → `UnknownCameraError`.
- Both errors surface through the readiness gate's `"unreachable"` path with the message carried through.

**Exempt tools (never advertise `camera`)**: `obsbot_devices`, `obsbot_capture_stop`, `obsbot_capture_list`, `obsbot_debug_probe`, `obsbot_capture_record`, `obsbot_capture_preview`. `obsbot_capture_snapshot` honors `camera` **only** for `source:"device"`; for `virtual`/`ndi` the pixel source is resolved by device name regardless of `camera`.

### 0.3 Constants (verbatim)

| Constant | Value | Notes |
|---|---|---|
| `clamp(v, min, max)` | `Math.min(max, Math.max(min, v))` | |
| `PROBE_VENDOR_SELECTOR` | `0x02` | Vendor XU selector: SET_CUR request AND GET_CUR reply mailbox. Protocol constant, not platform-specific. |
| `PROBE_QUERY_POLL_ATTEMPTS` | `8` | Reply mailbox retains the PREVIOUS reply until the new one lands; a single unvalidated read can return stale data. |
| `PROBE_QUERY_POLL_DELAY_MS` | `30` | Device populates the mailbox non-instantly; opcode-dependent (cached state like `AI_GET_QUICK_STATUS` sub-ms; persistent-storage reads like `UG_GET_SN` far slower). Measured darwin-arm64 2026-07-21, `UG_GET_SN` × 25 trials: no delay = 24 failures (96%); 30 ms delay = 0. |
| `GIMBAL_MAX_SPEED_DPS` | `150` | Hardware-measured 2026-07-21: 150/160/170 °/s all drove the gimbal linearly; 180/200/300 each moved it **EXACTLY 0°** yet still reported success — firmware **ignores** over-range speed rather than saturating. True cutoff in 170..180; 150 keeps margin and matches `obsbot_gimbal_move`'s yaw bound. |
| `PRESET_NAME_MAX` | `40` | Rename frame payload = `u32le(slot-1)` [4 bytes] + ASCII name, inside a fixed 60-byte frame whose payload region starts at offset 16; max payload 60−16=44 bytes → name ≤ 44−4 = 40 bytes. |
| `PRESET_READ_DEFAULTS` | `{ attempts: 3, backoffMs: [200, 500, 1000] }` | Backoff spans ~1.7 s, chosen against the observed ~1–2 s post-wake self-centering window. |
| `round2(deg)` | `Math.round(deg * 100) / 100` | Pan/tilt readback is a float carrying more digits than the hardware's ±1° accuracy justifies (e.g. `5.975277777777778` from an arc-second division); report at 2 decimals. |
| `ZOOM_SETTLE_DEFAULTS` | `{ pollMs: 100, timeoutMs: 3000, motionPollMs: 80 }` | `motionPollMs` is the sensitivity knob for steady-state detection: zoomPercent is an integer, so a beat of B ms detects any ramp faster than 1000/B pt/s. 80 ms ⇒ 12.5 pt/s vs a measured UVC ramp of ~42 pt/s (2026-07-25: full ratio 1→2 sweep took 2397 ms). |
| `ZOOM_SETTLE_TOLERANCE_PCT` | `1` | Target expressed on the `zoomPercent` 0–100 scale (`pct = (ratio − 1) * 100`), NOT raw device zoom units (those come from `zoomRange()`, per-device). |
| Snapshot default resolution | `640` (was 1024 `maxDim`) | JPEG is base64'd into a tool response an LLM reads; every pixel costs tokens. 640 → ~86 KB; 1920 → ~413 KB. |
| Snapshot max resolution | `1920` (sensor is 3840×2160) | 4K needs an explicit device activeFormat change (session preset alone does not do it — activeFormat governs and stays at 1080p), mutating shared device state another streaming app would feel; and a 4K frame ≈1.5 MB ≈ 2M base64 chars. Over-ceiling requests are **rejected**, not silently served 1080p. |
| Snapshot `settleMs` ceiling | `15000` (raised from 5000 on 2026-07-25) | Lazy-connecting sources (NDI Webcam Input) need 4–5 s for the first frame; 5000 sat right on that threshold. |

### 0.4 `resolveMagnification(status)` → `{ok:true, magnification}` \| `{ok:false, reason, zoomPercent?}`

The camera's total magnification relative to wide, from its reported state. A discrete FOV mode and a continuous zoom are two ways of writing to **one** scale:

- `status.fovMode === "unknown"` → `{ ok: false, reason: "unknown-fov" }` (status byte didn't decode — refuse rather than guess).
- `status.fovMode === "custom"` → `m = magnificationFromZoomRatio(1 + status.zoomPercent / 100)`; if `m` is not finite or `m < MIN_MAGNIFICATION (1)` or `m > MAX_MAGNIFICATION (4)` → `{ ok: false, reason: "implausible-zoom", zoomPercent }` (corrupt/garbled status byte, e.g. zoomPercent > 100). Else `{ ok: true, magnification: m }`.
- otherwise → `{ ok: true, magnification: FOV_MAGNIFICATION[status.fovMode] }` (wide 1, medium 1.15060, narrow 1.47073).

### 0.5 `refuseIfNot169(frameWidth, frameHeight)`

If `Math.abs(frameWidth/frameHeight − 16/9) > 0.02` → return
`{ ok:false, error: "frame WxH is not 16:9, but obsbot_capture_snapshot always returns 16:9 frames. This looks like frameWidth/frameHeight were transposed, or came from something other than that tool's result." }`; else `null`. 16:9 is preserved by the capture path at every resolution (verified at 256×144, 1280×720, 1920×1080). A transposed pair is worse than a bad aim for zoom-to-fit: it flips which axis `Math.min(frameWidth/width, frameHeight/height)` selects.

### 0.6 Zoom settle / steady-status helpers

**`waitForZoomSettle(t, targetRatio, opts)` → boolean.** `targetPct = (targetRatio − 1) * 100`. Loop: read status; if `|zoomPercent − targetPct| ≤ 1` → `true`. A read that throws is treated as "not settled yet" (by this point the gimbal move and zoom write have already happened — throwing would lose the target/ratio result). At deadline (`timeoutMs`, default 3000) → `false` (never throws). Poll interval `pollMs` = 100. `zoomPercent` tracks **actual travel**, not the commanded value (observed: commanding ratio 1.5 read back zoomPercent 33 in transit before settling at 50).

**`readSteadyStatus(t, opts)`** — two status reads `motionPollMs` (80 ms) apart. If the two `zoomPercent` values differ → `{ ok:false, fromPct, toPct }`. Else `{ ok:true, block, status }` where the SECOND (fresher) read is the one returned. Refusal text (`zoomMovingError(r, verb)`, verb = "aiming" or "framing"):
`"the zoom is still moving (zoomPercent read {fromPct} then {toPct}), so the frame you measured was captured at a magnification the camera has already left — {verb} from it would be wrong in proportion. Wait for the zoom to finish, take a fresh snapshot, and try again."`

### 0.7 `readFocus(t)` — used by `obsbot_status`. NEVER throws.

Focus is a standard UVC control, not a field of the 60-byte vendor status block (costs two extra reads). `camCtrlGet(CAMERA_CONTROL_FOCUS)` → `{value, flags}`. `flags === UVC_FLAG_AUTO` → mode `"auto"`; `=== UVC_FLAG_MANUAL` → `"manual"`; else `"unknown"`. Only in manual mode: read `camCtrlRange(CAMERA_CONTROL_FOCUS)` and report `focusPosition = rangeToPercent(value, min, max)` (same 0–100 scale `obsbot_focus_manual` writes; round-trips exactly — write 25, read 25). Under autofocus the device does NOT expose the motor: MEASURED 2026-07-25, value stayed pinned at the last written position across a 40° pan, a zoom 3.34x→1x, and 9 s of settling — a setpoint echo, so the field is **omitted** under auto. Any failure → `{ focusMode: "unknown" }`.

### 0.8 `frameSourceNote(source)` — returned as `note` on non-`device` aims

`"this aim was computed from a '{source}' frame, and is only correct if that feed is an unmodified pass-through of the camera (no rescale, crop, letterbox or reframing) at the same field of view — 1080p60 is a 1.214x crop of 1080p30 on this camera, and a compositor's canvas scaling is invisible in the picture. Verify once by commanding a known gimbal rotation and checking features move by the predicted number of pixels; a 'device' frame needs no such check."`

Measured 2026-07-25 (OBS → NDI): a dedicated uncomposited 1080p30 output matched the camera's frame at fill 1.0000, scale 1.03 ± 0.03 and framed correctly; the SAME chain via OBS's canvas measured 0.900 scale + 96 px offset — wrong by 11% plus a fixed bias — and looked identical in the reply.

### 0.9 Preset read path (selectors 12/13) — required behavior

Hardware-verified 2026-07-19. **NOT** the framed-reply model (recvVendor + parseFrame) — reads on the vendor reply path just return the flat status block for this device. `getPresetSlots(t)`:

1. `xuGetRaw(12, 60)` → block: `<count:u8> <slotIdx:u8> × count`.
2. If block is **all-zero**: a genuinely EMPTY device and a not-serving read BOTH return an all-zero selector-12 block (hardware-established under controlled conditions). Corroborate via `presetSubsystemServing(t)`: `recvStatus(60)` must be **non-zero AND** `decodeStatus(...).awake` (two-part deliberately — `decodeStatus` reports awake for an all-zero block since `awake === block[0x02] === 0`, so `awake` alone would let a dead link masquerade as empty). UVC liveness is NOT valid corroboration (`obsbot_gimbal_position` answers correctly even while the preset selectors aren't serving). If serving → return `assemblePresetSlots([])` and **skip the echo-write** (no cursor to reset).
3. Validate with `implausiblePresetListReason(block)` **BEFORE** the echo-write; if a reason → throw `` `preset list read implausible, refusing to reset the cursor: ${reason}` ``.
4. `decodePresetList(block)`; then **echo-write** the exact just-read bytes back to selector 12 (`xuRaw(12, block)`) — resets the entry cursor; echo is provably non-destructive; do NOT write zeros/synthesized bytes.
5. Walk `walkCount = Math.min(list.count, 3)` GETs of selector 13 (60 bytes each) via `decodePresetEntry`; stop at `e.end` (exhausted marker: status `0x02`). Independent guard against a garbage count causing up to 255 USB control transfers.
6. Consistency check (I1): sorted slot set from the walk (`e.slot − 1` values, deduped) must equal `list.slots` sorted; otherwise throw `` `preset list mismatch: selector 12 claims occupied slots [..] but the entry-cursor walk (selector 13) found [..]` ``. A false EMPTY is the dangerous direction for a create-once resource.
7. Return `assemblePresetSlots(per)`.

**`readPresetSlots(t, gate, {attempts:3, backoffMs:[200,500,1000]})`**: retry loop; on attempt i>0, sleep `backoffMs[min(i−1, len−1)]` then re-invoke the readiness `gate()` (re-probe: the failure mode chased is a device mid-transition; the wake it may send is a decoded, documented command). On a successful read where **every slot is empty**, immediately re-read; if the re-read disagrees → throw `"preset list unstable: one read reported every slot empty, an immediate re-read disagreed — refusing to treat this as empty"`. EMPTY is double-confirmed because it authorizes the irreversible create-once ADD; the inverse error is benign (delete becomes a no-op, update fails). Writes are never retried here.

---

## 1. Tools

Advertised set = all below, minus `obsbot_debug_probe` unless the server was started with `--debug`, minus `obsbot_gimbal_move_speed` when `process.platform === "linux"` (hidden from the list entirely, not refused at runtime — no live position feedback there means a speed×duration burst cannot be bounded against mechanical limits).

Common notation: `gate(camera)` = §2's `ensureReady`; if it returns `{ok:false,...}` the handler returns it verbatim. `reconnected: true` is appended to results only when the gate self-healed a disconnect this call.

### 1.1 `obsbot_devices`
- **Schema**: `{}` (no params, no `camera`).
- **Handler**: `mgr.listCameras()` → `{ cameras: [...] }`. Each entry `{ serial?, locationId?, name, status, reason? }`; `status` ∈ `available` (free to bind) | `bound` (opened by this process) | `busy` (could not be opened+identified — usually another process holds it, but also covers a camera that opened yet would not answer; `reason` carries the underlying error and distinguishes those).

### 1.2 `obsbot_wake`
- **Schema**: `{ camera? }`.
- **Handler**: get transport → `sendVendor(encodeSetRunStatus("run").buildFrame(t.nextSeq()))` → `{ ok:true, state:"run" }`. **MOVES the camera**: waking un-stows the gimbal back to level (pitch ~0). No readiness gate (this IS the wake).

### 1.3 `obsbot_sleep`
- **Schema**: `{ camera? }`.
- **Handler**: `sendVendor(encodeSetRunStatus("sleep")...)` → `{ ok:true, state:"sleep" }`. **MOVES the camera**: stows face-down at roughly pitch **84°**, so `obsbot_gimbal_position` will read ~84.

### 1.4 `obsbot_gimbal_move`
- **Schema**: `{ yaw: number, pitch: number, roll: number = 0, camera? }`.
- **Handler**: clamp yaw to ±`GIMBAL_YAW_LIMIT_DEG` (150), pitch to ±`GIMBAL_PITCH_LIMIT_DEG` (90); roll passed unclamped. `gate(camera)`; on ok → `t.gimbalSet(yaw, pitch, roll)` → `{ yaw, pitch, roll [, reconnected:true] }`. Absolute positioning, 1:1 degrees, hardware-verified. Sign convention: **+yaw pans camera-LEFT, +pitch tilts DOWN**.

### 1.5 `obsbot_gimbal_move_speed` *(hidden on Linux)*
- **Schema**: `{ yaw: number, pitch: number, roll: number = 0, autoStopMs: number = 800, camera? }` — yaw/pitch/roll in **degrees per second**.
- **Handler**: clamp all three speeds to ±150 °/s (`GIMBAL_MAX_SPEED_DPS`; see §0.3 — firmware silently ignores over-range instead of saturating). `gate`; on ok → `t.gimbalSpeed(yaw, pitch, roll, autoStopMs)` → `{ ok:true, yaw, pitch, stopped: autoStopMs > 0 [, reconnected] }`. Linearity measured 2026-07-21 against live readback: 10 °/s×1000 ms → 10°, 60×500 → 29°, 120×300 → 35°, 170×300 → 49°. **Hardware quirk**: firmware velocity-yaw is inverted relative to position-yaw (vendor `AI_SET_GIM_SPEED` +yaw drives right); the transport's `gimbalSpeed` compensates per-platform so the tool's +yaw = camera-left matches `obsbot_gimbal_move`.

### 1.6 `obsbot_gimbal_recenter`
- **Schema**: `{ camera? }`.
- **Handler**: `gate`; `t.gimbalRecenter()` → `{ ok:true [, reconnected] }`. Returns as soon as the command is sent — gimbal may still be moving; poll `obsbot_gimbal_position` for arrival.

### 1.7 `obsbot_gimbal_position`
- **Schema**: `{ camera? }`.
- **Handler** (no gate): `camCtrlGet(CAMERA_CONTROL_PAN)`, `camCtrlGet(CAMERA_CONTROL_TILT)` → `{ yaw: round2(pan.value), pitch: round2(−tilt.value) }`. **UVC pan is degrees, same sign as yaw (+ = camera-left); UVC tilt is degrees but positive = up, so NEGATE to +pitch = down.** Live hardware readout ±1°, valid during a move, reflects motion the host did not command (speed moves, recenter, tracking). On Linux: last-*commanded* value, not live (see §5).

### 1.8 `obsbot_zoom_uvc`
- **Schema**: `{ ratio: number, camera? }`.
- **Handler** (no gate): `ratio = clamp(ratio, 1.0, 2.0)`; `zoomRange()` → `{min,max}`; `zoomSet(zoomRatioToUnits(ratio, min, max))`; then `settled = waitForZoomSettle(t, ratio)` → `{ ok:true, ratio, settled }`. Snaps exactly to target (unlike vendor path). Full 1.0→2.0 sweep ≈ 2.4 s. `settled:false` = didn't arrive within timeout; command was still sent.

### 1.9 `obsbot_zoom_vendor`
- **Schema**: `{ ratio: number, speed: number = 0, camera? }`.
- **Handler** (no gate): `ratio = clamp(ratio, 1.0, 2.0)`; `speed = clamp(round(speed), 0, 255)` (0 = device default, 1–10 slow→fast, 255 = maximum); `sendVendor(encodeZoomWithSpeed(Math.round(ratio*100), speed).buildFrame(nextSeq()))` → `{ ok:true, ratio, speed }`. **Deliberately no settle wait**: this path's ratio scale differs from UVC's and may not land exactly on target, so `waitForZoomSettle` would report `settled:false` on a perfectly good zoom; and "zoomPercent stopped changing" reads as stopped during the ~140 ms pre-ramp lag — a worse lie. Known limitation: at `ratio: 2.0` the vendor path framed tighter than UVC in a hardware snapshot comparison; undetermined whether it's a scale factor or different physical ranges.

### 1.10 `obsbot_ai_track`
- **Schema**: `{ enabled: bool, mode: enum = "normal", camera? }`. `mode` ∈ framings `AI_FRAMING_MODES` (`normal | upper-body | close-up | headless | lower-body`) ∪ scene modes `AI_SCENE_MODES` (`group | whiteboard | desk | hand`). Scene modes imply enabled:true; disable via `enabled:false` (cancels tracking).
- **Handler**: `gate`. Snapshot `before = readAiMode()` (decodeStatus of recvStatus; a throwing read → `"unknown"`). Build payload: `!enabled` → `encodeAiMode("none")`; scene mode → `encodeAiMode(mode)`; framing → `encodeAiTracking(true, mode)`. Write via **raw `xuRaw(UVC_XU_SELECTOR, payload)`** — OBSBOT Center toggles tracking with a raw uvcExt write to **selector 6**, NOT a framed V3 command (which the Tiny 2 ACKs but ignores). Payload byte[2] = work mode, byte[3] = human framing sub-mode; a scene mode is its own work mode. Then `want = enabled ? mode : "no-tracking"`; `verifyFraming(readAiMode, want, before)` → return `{ ok:true, enabled, mode, verified, matched [, reconnected] }`.

**`verifyFraming` (dist/mcp/framing.js)** — defaults `attempts=30`, `intervalMs=200` (6 s ceiling; a mid-switch **m=6 transient** decodes to "unknown" and was observed parking ~3–4 s; a 2.4 s window produced flaky false-negative `matched:false`). Loop (sleep before every read except the first): read aiMode; if `=== want` → `{verified: aiMode, matched: true}` (early exit, normal landing ~1–4 s); if `!== "unknown"` **and** `!== before` → `{verified: aiMode, matched: false}` (settled to a different stable framing, e.g. no subject → "no-tracking"; polling longer won't help); else keep polling. Window expiry → `{verified: last, matched: false}`. Best-effort: the write already succeeded.

### 1.11 `obsbot_ai_track_speed`
- **Schema**: `{ speed: enum(AI_TRACK_SPEEDS) = "standard"|"sport", camera? }`.
- **Handler** (no gate): `sendVendor(encodeAiTrackSpeed(speed)...)` → `{ ok:true, speed }`.

### 1.12 `obsbot_focus_face`
- **Schema**: `{ enabled: bool, camera? }`.
- **Handler** (no gate): `sendVendor(encodeFaceFocus(enabled)...)` → `{ ok:true, enabled }`. Face-priority autofocus.

### 1.13 `obsbot_status`
- **Schema**: `{ camera? }`.
- **Handler** (no gate): `block = recvStatus()`; return `{ ok:true, ...decodeStatus(block), ...(await readFocus(t)), ...(debug ? { raw: block.toString("hex") } : {}) }`. Decoded fields: `awake, hdr, faceAe, aiMode (no-tracking|normal|upper-body|close-up|headless|lower-body|desk|whiteboard|hand|group|unknown), trackSpeed (standard|sport|unknown), fovMode (wide|medium|narrow|custom|unknown — custom = continuous zoom overrode the discrete modes), zoomPercent (0–100)`, plus `focusMode (auto|manual|unknown)` and `focusPosition` (manual only; §0.7). On any read error → `{ ok:false, error: "could not read camera status: ..." }`. `raw` (full 60-byte block as hex) only under `--debug`.

### 1.14 `obsbot_debug_probe` *(advertised only under `--debug`; no `camera` param — current diagnostics transport)*
- **Schema**: `{ mode: "get"|"set"|"query", selector?: int 0–255, length?: int 1–1024, hex?: string, opcode?: string, payloadHex?: string }`.
- **Handler**: get transport (no selector, no gate); everything inside try/catch → `{ ok:false, error }`.
  - `get`: `sel = selector ?? 0x06`; `xuGetRaw(sel, length ?? 128)` → `{ ok:true, selector, len, raw: hex }`.
  - `set`: requires `selector` and `hex` else `{ok:false, error:"mode 'set' requires selector and hex"}`; `xuRaw(selector, Buffer.from(hex,"hex"))` → `{ ok:true, selector, sent: hex }`.
  - `query`: `payload = payloadHex ? bytes : empty`; `name = opcode ?? "AI_GET_QUICK_STATUS"`; look up `OP_BY_NAME`; if missing or `wireCmd === null` or `receiver === null` → `{ok:false, error: 'opcode "name" is not a sendable V3 command'}`. Frame: **a pure GET (no payload) must be framed header-only, flags `0x01` (`encodeVendorGet`) — the only flavour this device answers for a GET; the SET flavour (flags `0x25`, `encodeVendorProbe`) returns a stale echo when there's no payload.** With `payloadHex`, use `encodeVendorProbe` (flags 0x25). `seq = nextSeq()`; `xuRaw(0x02, frame)`; then up to **8** polls: sleep **30 ms**, `xuGetRaw(0x02, length ?? 60)`, `parseFrame(raw)`; accept only when it parses cleanly **AND** `p.cmd === op.wireCmd && p.seq === seq` (mailbox staleness hazard). Success → `{ ok:true, opcode, sentFrame: hex, replyHex: hex, parsed: { cmd: "0x"+cmd.toString(16).padStart(4,"0"), receiver, payloadHex } }`. Exhausted → `{ ok:false, error: "no valid reply for {name} (seq {seq}) after 8 attempts", sentFrame }`.

### 1.15 `obsbot_image_fov`
- **Schema**: `{ fov: enum(FOV_TYPES) = "wide"|"medium"|"narrow", camera? }` (wide 86°, medium 78°, narrow 65° — the marketing diagonal figures; the geometry module uses measured horizontal values, §4).
- **Handler** (no gate): `xuRaw(UVC_XU_SELECTOR, encodeFov(fov))` → `{ ok:true, fov }`.

### 1.16 `obsbot_image_hdr`
- **Schema**: `{ enabled: bool, camera? }`.
- **Handler** (no gate): `xuRaw(UVC_XU_SELECTOR, encodeHdr(enabled))` → `{ ok:true, enabled }`.

### 1.17 `obsbot_focus_auto`
- **Schema**: `{ camera? }`.
- **Handler** (no gate): `camCtrlSet(CAMERA_CONTROL_FOCUS, 0, UVC_FLAG_AUTO)` → `{ ok:true, mode:"auto" }`.

### 1.18 `obsbot_focus_manual`
- **Schema**: `{ position: number 0–100 = 50, camera? }` (near→far).
- **Handler** (no gate): `camCtrlRange(CAMERA_CONTROL_FOCUS)`; `value = percentToRange(position, min, max)`; `camCtrlSet(CAMERA_CONTROL_FOCUS, value, UVC_FLAG_MANUAL)` → `{ ok:true, mode:"manual", position, value }`.

### 1.19 `obsbot_aim_at_pixel`
- **Schema**: `{ x: finite, y: finite, frameWidth: finite ≥1, frameHeight: finite ≥1, source: "device"|"virtual"|"ndi" = "device", camera? }`. `source` is a **declaration**, not a selector — these tools never fetch a frame.
- **Handler**, in order:
  1. `refuseIfNot169(frameWidth, frameHeight)` → return refusal if any.
  2. If `x<0 || x>frameWidth || y<0 || y>frameHeight` → `{ ok:false, error: "pixel (x,y) is outside the WxH frame" }`.
  3. `gate(camera)`; propagate `ok:false`. If `ready.woke` → `{ ok:false, error: "the camera was asleep and waking it moved the gimbal, so the frame you measured no longer matches where the camera is pointing. Take a fresh snapshot and aim again." }`.
  4. `readSteadyStatus` (§0.6); if not steady → `{ ok:false, error: zoomMovingError(steady, "aiming") }`.
  5. If `status.aiMode === "unknown"` → refuse ("the status block didn't decode this read — this can happen during a brief mode-switch transient; retry the aim").
  6. If `status.aiMode !== "no-tracking"` → `{ ok:false, error: "AI tracking is active ({aiMode}); it moves the gimbal itself and would fight the aim. Disable it with obsbot_ai_track {enabled:false} first." }`.
  7. `resolveMagnification(status)` (§0.4); on `"unknown-fov"` the error surfaces the raw byte: `block[0x11]` (**offset `0x11` = STATUS_OFF_FOV_MODE** in the 60-byte status block), formatted `0x`+2-digit hex; tells caller to set a known mode with `obsbot_image_fov`. On `"implausible-zoom"` the error names `zoomPercent` and range [1, 4].
  8. Read live pose: `yaw = camCtrlGet(PAN).value`, `pitch = −camCtrlGet(TILT).value`.
  9. `aim = aimAtPixel(x, y, {width:frameWidth, height:frameHeight}, {magnification}, {yaw, pitch})` (§4).
  10. If `aim.overTheTop` → refuse (moving would slew ~150° into the opposite corner of the room while reporting `clamped:true`; error tells caller to tilt toward the pixel first with `obsbot_gimbal_move`, then re-aim).
  11. `gimbalSet(aim.target.yaw, aim.target.pitch, 0)`.
  12. Return `{ ok:true, target, offset, clamped, fovMode: status.fovMode, current: {yaw, pitch}, source, [note: frameSourceNote(source) if source !== "device"], [reconnected] }`.

### 1.20 `obsbot_zoom_to_fit`
- **Schema**: `{ x, y, width, height, frameWidth ≥1, frameHeight ≥1 (all finite), margin: finite ≥0 = 0.1, source = "device", camera? }`. width/height/x/y deliberately unconstrained beyond finite: a negative width is valid **input** (schema's job), not a valid **region** (handler's job — structured `ok:false` instead of a schema throw).
- **Handler**, in order:
  1. `refuseIfNot169` (before region check, matching aim_at_pixel's ordering).
  2. Region bounds, edges included (a region that already IS the full frame must pass — the full-frame fit test depends on it): refuse if `width<=0 || height<=0 || x<0 || y<0 || x+width>frameWidth || y+height>frameHeight` → `{ ok:false, error: "region (x,y) WxH is not inside the WxH frame, or has non-positive width/height." }`.
  3. Same gate / woke / steady-status / aiMode-unknown / tracking-active / resolveMagnification refusals as aim_at_pixel (tracking message says "fight the framing").
  4. Live pose as above. `centerX = x + width/2`, `centerY = y + height/2`. `aim = aimAtPixel(centerX, centerY, frame, {magnification}, {yaw, pitch})` — aim uses the **CURRENT** magnification (must match the optics the frame was captured at, not the new fitted zoom). Over-the-top → refuse.
  5. **Fit formula**: `requiredRaw = (magnification * Math.min(frameWidth/width, frameHeight/height)) / (1 + margin)`. The **MIN** is deliberate: the axis needing LESS extra zoom sets the target, keeping the OTHER axis from overflowing — the whole region stays visible instead of being cropped. Aspect ratio and `VERTICAL_TANGENT_CORRECTION` do NOT appear: writing the fit condition on each axis they scale tanH and tanV identically on both sides and cancel.
  6. `requiredMagnification = clamp(requiredRaw, 1, 4)`; `fitClamped = requiredMagnification !== requiredRaw`; `ratio = zoomRatioFromMagnification(requiredMagnification)` = `(m+2)/3`.
  7. **Move BEFORE zoom** (`gimbalSet(aim.target.yaw, aim.target.pitch, 0)`): zoom is centre-preserving but not target-preserving; zooming first can push the region's centre out of frame.
  8. `zoomRange()`; `zoomSet(zoomRatioToUnits(ratio, min, max))`; `settled = waitForZoomSettle(t, ratio)`.
  9. Return `{ ok:true, target, ratio, magnification: requiredMagnification, clamped: fitClamped || aim.clamped, settled, source, [note if non-device], [reconnected] }`. `settled:false` is a result, not an error.

### 1.21 `obsbot_preset_list`
- **Schema**: `{ camera? }`.
- **Handler**: `gate`; `readPresetSlots(t, () => gate(camera), presetRead)` (§0.9) → `{ ok:true, slots [, reconnected] }`; thrown errors → `{ ok:false, error }`. Slots include occupied/empty, name, pose in degrees.

### 1.22 `obsbot_preset_save`
- **Schema**: `{ slot: 1|2|3, camera? }`.
- **Handler**: `gate`; read slots; if `before[slot-1].occupied` → `{ok:false, error:"slot N is occupied; update or delete first"}` (slots are **create-once**; no overwrite). Read pose exactly as `obsbot_gimbal_position` does (pan → yaw; −tilt → pitch); `pose = { pan: yaw, tilt: pitch, roll: 0, zoom: 1 }` — **zoom hardcoded to 1**: the transport exposes no zoom getter, deliberate not an oversight. `sendVendor(encodePresetAdd(nextSeq(), slot, pose))`. Pre-commit errors → `{ok:false, error: "preset save failed: ..."}`. Post-commit: re-read; if slot not occupied → `{ ok:false, error:"verification failed", expected:"occupied", actual:"empty" }`; if the verify read itself throws → `{ ok:false, error: "preset saved to slot N but verification failed: ..." }` (must say the write committed, or a retry hits "slot occupied" with no clue). Success → `{ ok:true, slot: after[slot-1] [, reconnected] }`.

### 1.23 `obsbot_preset_recall`
- **Schema**: `{ slot: 1|2|3, camera? }`.
- **Handler**: `gate`; read slots; empty slot → `{ok:false, error:"slot N is empty; save first"}`. `sendVendor(encodePresetRecall(nextSeq(), slot))`. Then, if the slot has a saved pose, **also** issue `gimbalSet(pose.pan, pose.tilt)`: the vendor-frame recall physically moves the gimbal but **corrupts V4L2 pan_absolute/tilt_absolute on Linux**; the follow-up gimbalSet both moves to the known target and restores the V4L2 register. Re-read and verify still occupied → `{ ok:true, slot: after[slot-1] [, reconnected] }`. Verification only confirms occupancy, not pose arrival — gimbal may still be moving. Any throw → `{ok:false, error}`.

### 1.24 `obsbot_preset_update`
- **Schema**: `{ slot: 1|2|3, camera? }`.
- **Handler**: `gate`; slot must be occupied ("slot N is empty; save first"). Capture `previous = before[slot-1].pose` (the pose about to be destroyed — the caller's only restore path; the device keeps no history and UPDATE is irreversible). Read live pose (same as save, zoom:1); `sendVendor(encodePresetUpdate(nextSeq(), slot, pose))`. Pre-commit error → `"preset update failed: ..."`. Post-commit verify occupied; verify-throw → `"preset updated in slot N but verification failed: ..."`. Success → `{ ok:true, slot: after[slot-1], previous [, reconnected] }`.

### 1.25 `obsbot_preset_rename`
- **Schema**: `{ slot: 1|2|3, name: string, camera? }`.
- **Handler**: `gate`; slot must be occupied. `clean = name.slice(0, 40)` (`PRESET_NAME_MAX`; wire-frame limit, §0.3). `sendVendor(encodePresetSetName(nextSeq(), slot, clean))`. **Encoding note**: the read path base64-decodes the stored name, but the captured OBSBOT Center rename frame carried raw ASCII ("Preset1", "3reset2A"), so the write side sends raw ASCII — whether the device expects ASCII or base64 on write is NOT hardware-confirmed. Re-read; if `after[slot-1].name !== clean` → `{ ok:false, error:"verification failed", expected: clean, actual: after[slot-1].name }`. Success → `{ ok:true, slot: after[slot-1] [, reconnected] }`.

### 1.26 `obsbot_preset_delete`
- **Schema**: `{ slot: 1|2|3, camera? }`.
- **Handler**: `gate`; already-empty slot → `{ok:false, error:"slot N is already empty"}`. Capture `destroyed = { name, pose }` from the guard read (the caller's ONLY route back if the wrong slot was named). `sendVendor(encodePresetDelete(nextSeq(), slot))`; set `sent = true`. Re-read; still occupied → `{ ok:false, error:"verification failed", expected:"empty", actual:"occupied" }`. Success → `{ ok:true, deleted: destroyed [, reconnected] }`. On a throw **after** `sent` → `{ ok:false, error: "delete was sent and may have been applied, but verification failed: ...", committed: "unknown" }`; before → `{ok:false, error}`.

### 1.27 `obsbot_image_wb_auto`
- **Schema**: `{ camera? }`.
- **Handler** (no gate): `procAmpRange(VIDEO_PROCAMP_WHITE_BALANCE)`; `procAmpSet(VIDEO_PROCAMP_WHITE_BALANCE, min, UVC_FLAG_AUTO)` (value = range minimum) → `{ ok:true, mode:"auto" }`.

### 1.28 `obsbot_image_wb_manual`
- **Schema**: `{ temperature: number = 5000, camera? }` (Kelvin).
- **Handler** (no gate): `value = clamp(Math.round(temperature), min, max)` from `procAmpRange`; `procAmpSet(..., value, UVC_FLAG_MANUAL)` → `{ ok:true, mode:"manual", temperature: value }` (the clamped value).

### 1.29 `obsbot_image_adjust`
- **Schema**: `{ control: enum(IMAGE_CONTROLS) = brightness|contrast|hue|saturation|sharpness|gain|backlight-compensation, level: number 0–100, camera? }`.
- **Handler** (no gate): `property = IMAGE_CONTROL_PROP[control]`; `procAmpRange(property)`; `value = percentToRange(level, min, max)`; `procAmpSet(property, value, UVC_FLAG_MANUAL)` → `{ ok:true, control, level, value }`. **`gain` and `backlight-compensation` are NOT implemented on the Tiny 2** (reported as zero-length controls) and are refused with an error rather than silently doing nothing; the other five work.

### 1.30 `obsbot_image_exposure_auto`
- **Schema**: `{ priority?: "global"|"face", camera? }`.
- **Handler** (no gate): **mode and value go in ONE command** — `CAM_SET_EXPOSURE_TINY2` with a 5-byte `[mode][value]` payload; the separate `CAM_SET_EXPOSURE_MODE` command is **inert** on this device, and a 4-byte value payload is **silently discarded**. Device exposure range is **1..2500** (read from `CAM_GET_EXPOSURE_RANGE_TINY2`; a previous 0..65535 figure came from the Tiny4Linux reference and does not match this hardware). This branch carries no `level`, so `raw = percentToRange(50, 1, 2500)` (the pre-split default 50% — the device only acts on it once manual mode is selected). `sendVendor(encodeSetExposure(false, raw)...)`. If `priority` given: `xuRaw(UVC_XU_SELECTOR, encodeFaceAe(priority === "face"))` (sel-6 uvcExt write applied after auto-exposure is on; readback surfaces at **status offset `0x07`**) → `{ ok:true, mode:"auto", priority }`; else `{ ok:true, mode:"auto" }`. Rationale: the standard UVC/IAMCameraControl/V4L2 exposure path is a **stub** on the Tiny 2.

### 1.31 `obsbot_image_exposure_manual`
- **Schema**: `{ level: number 0–100 = 50, camera? }` (0 darkest → 100 brightest).
- **Handler** (no gate): `raw = percentToRange(level, 1, 2500)`; `sendVendor(encodeSetExposure(true, raw)...)` → `{ ok:true, mode:"manual", level, raw }` (`raw` = device-native value, diagnostics only; `level` is the number to reason with).

### 1.32 `obsbot_capture_snapshot`
- **Schema**: `{ resolution: number 256–1920 = 640 (longest edge, px), quality: number 1–100 = 80 (JPEG), settleMs: number 0–15000 = 600, source: "device"|"virtual"|"ndi" = "device", camera? (honored for device only) }`.
- **Handler**, in order:
  1. Get transport for `camera`.
  2. If `source !== "device"`: `mgr.list()` and find a device whose name matches `/OBSBOT Virtual Camera/i` (virtual) or `/NDI Webcam/i` (ndi); if none → text-only content: `"No '{source}' video source found (is OBSBOT Center / NDI running?)."`. Else `path = match.path`.
  3. **LOCAL PATCH — auto-wake (REQUIRED BEHAVIOR)**: a snapshot of a sleeping camera is always garbage (the lens is stowed facing down). Try `decodeStatus(await recvStatus())`; if `!awake` → `sendVendor(encodeSetRunStatus("run").buildFrame(nextSeq()))` then sleep **1500 ms** (gives the gimbal time to un-stow). Any status-read failure is swallowed — it **must not block the capture attempt**; proceed regardless.
  4. `snap = t.snapshot({ path, maxDim: resolution, quality, settleMs })` — the helper's **wire field is still `maxDim`** (shared with Windows/Linux helpers); `resolution` is only the tool-facing name.
  5. Return content array: `[{ type:"image", data: snap.base64, mimeType: snap.mime }, { type:"text", text: JSON.stringify({ width, height, source, sourceFormat? }) }]`. `sourceFormat` (e.g. `MJPG 1920x1080@30.00`) included only when the platform helper reports it (Windows only per README; absent = unknown) — frame rate picks the field of view on this camera, so the negotiated format is part of what a pixel means.
  6. `CameraBusyError` → text-only content: `"Camera is in use by another application. Close it (or try source:'virtual' or 'ndi' if OBSBOT Center is running), then retry."`. Any other error rethrows.
- Description tells the caller to ensure focus first (`obsbot_focus_auto`) unless otherwise directed. Device-path snapshot negotiates MJPG 1920×1080@30 (§4 header comment), does not need ffmpeg (native helper).

### 1.33 `obsbot_capture_record` *(no `camera`)*
- **Schema**: `{ durationSec?: number > 0, audio: bool = true, outputPath?: string, source = "device" }`.
- **Handler**: requires a configured capture manager (`needCapture()`; throws `"capture manager not configured"` otherwise). `startRecord({source, durationSec, audio, outputPath})` → `{ ok:true, sessionId: s.id, outputPath: s.outputPath, durationSec: s.durationSec }`. `CaptureError` → text content of the message; any other error rethrows. Open-ended recordings auto-stop after **60 min**; audio uses the OBSBOT mic; default output dir `~/Videos/OBSBOT` on **every** platform (including macOS, where that is NOT the usual `~/Movies`). Needs **ffmpeg**.

### 1.34 `obsbot_capture_preview` *(no `camera`)*
- **Schema**: `{ source = "device" }`.
- **Handler**: `startPreview({source})` → `{ ok:true, sessionId }`; `CaptureError` → text. Needs **ffplay**. `device` pinned to 1080p60 MJPEG for smooth motion (costs ~21% field of view vs snapshot — see §4); `virtual`/`ndi` negotiate (neither offers mjpeg; pinning would prevent the device opening at all).

### 1.35 `obsbot_capture_stop`
- **Schema**: `{ sessionId: string }`.
- **Handler**: `capture.stop(sessionId)` → `{ ok:true, ...result }`; `CaptureError` → text. Recordings finalize gracefully so the MP4 is valid.

### 1.36 `obsbot_capture_list`
- **Schema**: `{}`.
- **Handler**: `{ sessions: capture.list() }` (id, kind, source, output path, start time).

---

## 2. Readiness gating (`dist/mcp/ready.js`)

`ensureReady(getTransport, reconnect?, opts?)`, defaults `{ pollMs: 200, wakeTimeoutMs: 2500, settleMs: 300 }`.

A single status read is a **3-way probe**: throws ⇒ unreachable; else reports awake. `readAwake = decodeStatus(await t.recvStatus()).awake`.

1. `getTransport()` throws → `{ ok:false, reason:"unreachable", error: "camera not found: ..." }`.
2. `readAwake` throws (device likely unplugged): if no reconnect controller → `{ ok:false, reason:"unreachable", error: "camera not reachable: ..." }`. Otherwise self-heal **once**: `reconnect.invalidate()`, re-`getTransport()`, re-probe; still failing → unreachable with the second error.
3. Asleep: set `woke = true`; send wake (`encodeSetRunStatus("run").buildFrame(nextSeq())`); poll every `pollMs` (200 ms) up to `wakeTimeoutMs` (2500 ms); a read that throws during the poll is a transient — keep polling to the deadline. Never wakes → `{ ok:false, reason:"wake-timeout", error: "camera did not wake within timeout" }`. On wake, sleep `settleMs` (300 ms) — let the gimbal finish rising before driving it.
4. Return `{ ok:true, transport, reconnected: reconnect?.takeReconnected() ?? false, woke }`.

The gated command is only sent by the caller after `ok:true` — a gate failure never touches the gimbal. `msg(e)` helper: `e instanceof Error ? e.message : String(e)` (a non-Error throw would otherwise render `"undefined"`).

Tools that use the gate: gimbal_move, gimbal_move_speed, gimbal_recenter, ai_track, aim_at_pixel, zoom_to_fit, all six preset tools. Tools that do NOT: devices, wake, sleep, zoom_uvc, zoom_vendor, ai_track_speed, focus_face, status, debug_probe, image_fov, image_hdr, focus_auto/manual, wb_auto/manual, image_adjust, exposure_auto/manual, all capture tools (snapshot has its own auto-wake instead, §1.32.3).

---

## 3. Result rendering (`dist/mcp/render.js`)

`renderToolResult(result)`:
- If `result` is a non-null object with an **array** `content` property → passed through untouched (this is how `obsbot_capture_snapshot` returns `{type:"image", data:<base64>, mimeType}` blocks, and how capture/busy errors return plain text blocks).
- Otherwise → `{ content: [{ type: "text", text: JSON.stringify(result) }] }` — every control tool's plain object is serialized to a single JSON text block. Structured failures are ordinary `{ok:false, error}` objects rendered the same way (they are not MCP protocol errors); only genuinely unexpected exceptions propagate out of handlers.

---

## 4. Aiming geometry (`dist/geometry/aim.js`) — complete

Pure functions, no I/O. **Degrees in the public API, radians internal only.** `toRad(d) = d·π/180`, `toDeg(r) = r·180/π`.

### 4.1 FOV model

| Export | Value | Provenance |
|---|---|---|
| `WIDE_HFOV_DEG` | `67` | Horizontal FOV of the **capture stream** at the wide setting. Measured 2026-07-25 by solving camera intrinsics from pure gimbal rotations (rotation-only homography H = K·R·K⁻¹, no scene-depth dependence — the gimbal angle is the ruler). Six rotations (pitch ±10, ±20; yaw ±10), 313–1243 inliers each → fx = 1452–1455 px on a 1920-wide frame (0.2% spread) → HFOV 66.84–66.90, rounded 67; independently reproduces a 66.4 pan-and-track measurement. Head-to-head: a target at u = +0.91 left yaw residual −0.823° under the old 68 vs −0.274° under 67. One anchor + measured ratios rather than three ±3° absolutes (the ratios are ~60× more precise than the absolutes). |
| `FOV_MAGNIFICATION` | `{ wide: 1, medium: 1.15060, narrow: 1.47073 }` | Linear magnification of each FOV setting relative to wide; ratios good to ~0.05% (similarity-transform fits, 275/683 inliers, 0.48/0.44 px residual). **The continuous zoom writes to this same scale rather than multiplying on top**: `narrow` + zoom 1.5 measures 2.509, same as `wide` + 1.5 (2.501), not 1.47 × 2.5 — which is why setting zoom ratio 1.0 does not reliably clear `custom` (same optical state as `wide`). |
| `HORIZONTAL_FOV_DEG` | per mode: `2·toDeg(atan(tan(toRad(67/2)) / FOV_MAGNIFICATION[mode]))` | Derived. |
| `VERTICAL_TANGENT_CORRECTION` | `0.957` | Empirical, on top of the aspect-derived vertical half-angle. Square-pixel geometry says tan(V) = tan(H)·(height/width) (0.5625 at 16:9); hardware says the vertical field is ~4.3% shorter. From the same intrinsics solve: fy = 1502–1520 px → factor 0.957–0.967, rounded 0.957. **REPLACES 0.898, which was 7% low** (came from a measurement recorded as inconclusive). Up/down asymmetry is ~1% (0.957 from up-tilt alone, 0.967 from down-tilt) — one constant suffices; do NOT reintroduce a two-branch vertical constant (principal point centred, cx/cy within a few px; radial distortion negligible, k1 ≈ −0.02). Head-to-head: target at v = −0.83 left pitch residual −0.597° under 0.898 vs +0.072° under 0.957. Known limit: intrinsics fit carries 2.4–2.8 px rms (likely entrance pupil off the rotation axes; depth-dependent). Scope: 16:9 capture path only; a 4:3 path needs its own measurement. |
| `MIN_MAGNIFICATION` / `MAX_MAGNIFICATION` | `1` / `4` | Wide field, and the whole scale's extremes. |
| `magnificationFromZoomRatio(ratio)` | `3·ratio − 2` | MEASURED 2026-07-25: magnification is **linear** in the UVC ratio, holding to better than 0.05% at ratios 1.25/1.5/2.0. Ratio 2.0 ⇒ 4× linear. |
| `zoomRatioFromMagnification(m)` | `(m + 2) / 3` | Inverse. |
| `GIMBAL_YAW_LIMIT_DEG` | `150` | Mechanical yaw range on EVERY platform, not platform-conditional. 2026-07-25: commanded 145 reads back 145; 150 reads back 149. **Do not "fix" to 130**: the UVC `CT_PANTILT_ABSOLUTE` descriptor advertises ±468000 arcsec = ±130° — an under-reporting descriptor value, not where the gimbal stops; nothing clamps to it. |
| `GIMBAL_PITCH_LIMIT_DEG` | `90` | |

**Capture-format warning (load-bearing for reimplementation)**: at 1920×1080 the camera has two windows onto the sensor and **FRAME RATE selects between them, not the codec**. Measured 2026-07-25 at one pose/zoom: MJPEG@30 vs YUYV@30 = scale 1.00001, t (0.2, 0.0), 2382 inliers at 0.12 px (identical field to within a fifth of a pixel); **MJPEG@60 is a 1.214× crop of BOTH** (1.21422 and 1.21404). An earlier "MJPEG is a 1.201× crop of YUYV" note compared MJPEG@60 to YUYV@30 and charged the codec for what the frame rate did. These constants describe the **WIDE (30 fps) field** — what `obsbot_capture_snapshot` delivers (negotiates MJPG 1920×1080@30). `obsbot_capture_preview` pins 60 fps and therefore shows ~21% less — preview pixels are NOT interchangeable with snapshot pixels for aiming. Any future measurement must state pixel format AND frame rate. Scope: 16:9 capture at any resolution.

### 4.2 `halfAngleTangents(optics, frame)` (internal funnel — the ONE guard point)

```
if (!isFinite(m) || m < 1 || m > 4) throw RangeError("optics.magnification must be finite and within [1, 4], got m")
tanH = tan(toRad(67 / 2)) / optics.magnification
tanV = tanH * (frame.height / frame.width) * 0.957
```
The guard exists here because m = 0 divides to tanH = ∞ (atan silently resolves to 90°), negative m flips sign with no error, NaN propagates all the way through. Callers that pre-validate (`resolveMagnification`) never trip it; it is the backstop.

### 4.3 `halfAngles(optics, frame)` → `{ h: toDeg(atan(tanH)), v: toDeg(atan(tanV)) }` (degrees).

### 4.4 `pixelToOffset(x, y, frame, optics)` — PER-AXIS ONLY; tests-only now

```
u = 2x/frame.width − 1;   v = 2y/frame.height − 1
uEff = optics.mirrored ? −u : u
dYaw   = −toDeg(atan(uEff * tanH))
dPitch = +toDeg(atan(v   * tanV))
```
Rectilinear lens maps angle through a tangent: tan(θ) = u·tan(hfov/2). The linear approximation is exact at center and edge, always low between, peaking near **1.58°** at u ≈ 0.55 at wide — the difference between landing on target and visibly hunting; the tangent form is not optional. Signs: +yaw pans camera-LEFT while image x grows rightward → yaw term negated; +pitch tilts DOWN while image y grows downward → pitch term not; both hardware-verified. **Does NOT account for axis coupling** — adding dYaw/dPitch to a non-level pose reproduces the additive bug `aimAtPixel` fixed; do not use as a general "point the camera" answer.

### 4.5 `aimAtPixel(x, y, frame, optics, current)` → `{ target: {yaw, pitch}, offset: {dYaw, dPitch}, clamped, overTheTop }`

Composes a rotation instead of adding two scalars: the gimbal's yaw axis is **world-vertical**, so yawing while pitched sweeps a CONE; the rotations do not commute. `target = current + offset` is exact only at zero pitch or zero horizontal offset (adding them cost 0.98° at pitch 7.6 / yaw 31 on hardware, vs `pitch·(1 − cos yaw)` = 1.09 predicted). Camera coords: x right, y down, z forward.

```
u = 2x/W − 1;  v = 2y/H − 1;  uEff = mirrored ? −u : u
dx = uEff·tanH;  dy = v·tanV;  dz = 1;  n = sqrt(dx² + dy² + dz²)
cy = cos(rad(current.yaw));  sy = sin(rad(current.yaw))
cp = cos(rad(current.pitch)); sp = sin(rad(current.pitch))
// pitch first, about the camera's own x-axis (+pitch tilts DOWN):
px = dx/n;  py = (dy·cp + dz·sp)/n;  pz = (−dy·sp + dz·cp)/n
// then yaw, about the world vertical (+yaw pans camera-LEFT):
wx = px·cy − pz·sy;  wy = py;  wz = px·sy + pz·cy
rawPitch = deg(asin(clamp(wy, −1, 1)))
rawYawAtan2 = deg(atan2(−wx, wz))                       // range [−180, 180], −180 included
rawYaw = current.yaw + (((rawYawAtan2 − current.yaw + 180) mod 360 + 360) mod 360 − 180)
yaw   = clampTo(rawYaw, 150);  pitch = clampTo(rawPitch, 90)
hx = −sy;  hz = cy                                       // current horizontal heading
overTheTop = (hx·wx + hz·wz) < 0
return { target: {yaw, pitch},
         offset: { dYaw: rawYaw − current.yaw, dPitch: rawPitch − current.pitch },
         clamped: yaw !== rawYaw || pitch !== rawPitch,
         overTheTop }
```

Notes reproduced from source:
- **Yaw unwrapping**: pick the representative of rawYaw nearest the current yaw, so a target past +150 reads as +183 rather than −177 — without this a saturating aim clamps to the WRONG END of the range. It is a modulo wrap, **not** `Math.round((current.yaw − rawYaw)/360)`: at an exact 180° difference (dead behind the camera — happens for real at u = 0 aiming past vertical) JS `Math.round(0.5) = 1` rounds ties toward +∞ regardless of sign, silently flipping −180 to +180 and clamping on the wrong end; the wrap always resolves an exact tie to the lower edge of the window.
- **Saturation is REPORTED, not silent** (`clamped`) — a silent clamp presents as "the camera aimed and missed".
- **overTheTop**: the target ray's horizontal direction points opposite the current heading (negative dot of (hx,hz)·(wx,wz)); the only upright-image path is past vertical, and clamping yaw would slew toward the OPPOSITE side of the room — callers must refuse, not clamp-and-move.
- `current` must be where the camera actually was at frame capture (the module cannot verify). On Windows the caller-read pose is FLOORED to whole degrees, costing up to another degree — a defect in the pose source, not this function.
- Pitch reduces to the additive sum only at u = 0 (composed pitch is asin(dy/n) where n includes dx); yaw reduces exactly whenever pitch = 0.

---

## 5. README facts affecting behavior

- **Model support**: OBSBOT Tiny 2 only. Windows/macOS candidacy gated on Remo USB vendor ID + known product ID (`0x3564`/`0xFEF8`); software sources reporting no vid/pid (e.g. "OBSBOT Virtual Camera") rejected. Linux still matches by name (its helper doesn't report vid/pid yet), so other OBSBOTs may be *found* there — but the vendor command set is Tiny 2 specific. On macOS the virtual camera can't appear at all (IORegistry USB enumeration).
- **No env vars documented.** Configuration is CLI-only: the single flag `--debug` (exposes `obsbot_debug_probe` + `raw` hex on `obsbot_status`). MCP client config is a stdio server: `command: "obsbot-mcp"` or `node dist/index.js [--debug]`.
- **Platforms**: win32-x64 (hardware-verified, incl. disconnect recovery via `ERROR_DEV_NOT_EXIST`, same-port replug proactive re-bind); linux-x64 (hardware-verified moves/recenter via V4L2; position not live; `obsbot_gimbal_move_speed` unavailable); darwin-arm64 (fully verified incl. unaided unplug/replug recovery); darwin-x64 build-verified only, never executed. macOS 14+ required (`AVCaptureDeviceTypeExternal`), runtime verified on 26.5 only. macOS opens the USB *device* (not the UVC interfaces, which `UVCAssistant` owns exclusively — `USBInterfaceOpen`/`OpenSeize` fail `kIOReturnExclusiveAccess`), coexisting with normal webcam use. First macOS snapshot raises a camera permission prompt attributed to the MCP client app (the helper is a bare CLI with no bundle id); grant survives helper updates.
- **Architecture**: two control surfaces through the OS UVC stack — standard UVC controls (CT_ZOOM_ABSOLUTE, focus/exposure via IAMCameraControl, Pan/Tilt readback, IAMVideoProcAmp) and vendor XU commands (gimbal moves, recenter, wake/sleep, AI tracking, HDR, FOV). All issued via a spawned native helper (`obsbot-helper[.exe]`) over line-delimited JSON-RPC on stdin/stdout; codec/transport/manager/tools are shared JS.
- **Linux gimbal position is not live**: `uvcvideo` caches `CT_PANTILT_ABSOLUTE` and serves the cache (no `V4L2_CTRL_FLAG_VOLATILE`; the camera never sends UVC Control Change interrupts). Raw USB reads confirm the control genuinely tracks live position; detaching the kernel driver to read it breaks concurrent capture (streaming and control share one kernel-managed USB function), so it's not shipped. A kernel patch marking the control volatile is submitted upstream (July 2026, unmerged). Consequences: `obsbot_gimbal_position` reports last-commanded; `obsbot_gimbal_move_speed` is hidden; `obsbot_aim_at_pixel` is affected (depends on live pose).
- **AI tracking overrides manual moves** (Tiny 2's default on wake): a commanded pan/tilt executes then decays back to the tracked subject — camera behavior, not a bug; disable tracking for unopposed control.
- **USB hubs/docks**: camera may not enumerate at all through one (invisible even to `ioreg`/`system_profiler`); try direct connection before assuming a software fault.
- **Vendor reply mailbox unreliable for several seconds after a replug**: reproducibly, immediately after USB re-enumeration the mailbox returns the host's own echoed request (magic byte `0xaa` cleared to `0x00`, other bytes identical) — 22 failures in 80 attempts across the first 14 s vs 0/120 steady-state; `readSerial`/bind can fail on a healthy camera. Retrying works; arrival-driven re-bind retries on a bounded ladder. Ruled out: reply latency, wrong XU (exactly one, `bUnitID 2`), wrong `wLength` (every XU selector is 60 bytes by GET_LEN), other selectors (1–19 swept), sleep state, OBSBOT Center contention, stale process state, re-opening, sequence-counter restart.
- **Replug recovery matrix** (hardware-measured): macOS proactive for same- and different-port; Windows proactive same-port, next-tool-call different-port (arrival filter requires an already-enumerated path — also what stops the Tiny 2's audio interface appearing as a second camera); Linux next-tool-call always (no bus events). "Next tool call" = the following call detects the stale binding, prunes, re-binds; costs one failed call.
- **Two-camera operation not hardware-verified** (unit-tested against fakes only).
- **`obsbot_zoom_vendor` scale mismatch** vs UVC at the same ratio (framed tighter at 2.0); cause undetermined; use `obsbot_zoom_uvc` for exactness.
- **A preview holds the camera stream** — snapshots fail while one is open (Windows: `Camera is in use by another application`); gimbal control is unaffected (control transfers, not the stream). Stop the preview around each snapshot in the aim loop, or use `source:"virtual"/"ndi"` (safe for looking; safe for aiming only as a true pass-through — declare via `source` and read the returned `note`).
- **ffmpeg/ffplay** required for record/preview only (winget `Gyan.FFmpeg` / brew / apt `ffmpeg`); snapshot uses the native helper.
- Stale-owner trap when testing: concurrent clients elect a single owner process; a reloaded client forwards calls to an orphaned old owner while advertising the new tool list — kill orphans (`pgrep -af "obsbot.*dist/index.js"`) and confirm via changed *output*, not descriptions.

---

## 6. Tool classification

| Tool | Class | Justification |
|---|---|---|
| `obsbot_capture_snapshot` | **Essential** | The agent's eyes; the whole aim loop starts here, and the auto-wake patch makes it self-sufficient. |
| `obsbot_aim_at_pixel` | **Essential** | The core closed-loop "point at what I see" primitive; nothing else converts vision to motion. |
| `obsbot_zoom_to_fit` | **Essential** | Extends aiming to framing a region — the other half of visual servoing. |
| `obsbot_gimbal_move` | **Essential** | Absolute PTZ is the fallback whenever geometry-based aiming refuses (over-the-top recovery path depends on it). |
| `obsbot_gimbal_recenter` | **Essential** | Cheap known-good reset pose; recovery primitive. |
| `obsbot_gimbal_position` | **Essential** | Live pose readback; needed to confirm arrival and debug aims. |
| `obsbot_status` | **Essential** | Awake/tracking/FOV/zoom state gates nearly every other decision an agent makes. |
| `obsbot_zoom_uvc` | **Essential** | The exact, settle-verified zoom path; aim/fit tooling assumes its scale. |
| `obsbot_ai_track` | **Essential** | Tracking must be turn-off-able or aiming refuses; also the main hands-free mode. |
| `obsbot_wake` / `obsbot_sleep` | **Essential** | Explicit power/stow control; sleep is the only privacy-stow, wake the only un-stow. |
| `obsbot_devices` | **Essential** | Discovery + serials for `camera`; the only way to diagnose busy/bound state. |
| `obsbot_focus_auto` | **Essential** | Snapshot docs instruct calling it before capture; sharp frames gate everything visual. |
| `obsbot_capture_stop` / `obsbot_capture_list` | Nice-to-have | Only needed once record/preview exist; trivial session bookkeeping. |
| `obsbot_capture_record` | Nice-to-have | User-facing deliverable, not agent perception; needs ffmpeg. |
| `obsbot_capture_preview` | Nice-to-have | For a human watching; holds the stream and blocks snapshots, so agents mostly avoid it. |
| `obsbot_focus_manual` | Nice-to-have | Occasional deliberate-focus scenarios; auto covers the common case. |
| `obsbot_focus_face` | Nice-to-have | Refinement of autofocus for the person-on-camera case. |
| `obsbot_ai_track_speed` | Nice-to-have | A tuning knob on a mode the agent may already avoid. |
| `obsbot_image_fov` | Nice-to-have | Zoom tools subsume it (one magnification scale); still the documented recovery for `unknown-fov` refusals. |
| `obsbot_image_hdr` | Nice-to-have | Image-quality toggle, no workflow depends on it. |
| `obsbot_image_exposure_auto` / `_manual` | Nice-to-have | Useful for bad lighting; defaults are fine most of the time. |
| `obsbot_image_wb_auto` / `_manual` | Nice-to-have | Same — color tuning, rarely load-bearing. |
| `obsbot_image_adjust` | Nice-to-have | Five working ProcAmp sliders; cosmetic, and two enum values are refused on this hardware. |
| `obsbot_preset_list` / `_save` / `_recall` / `_update` / `_rename` / `_delete` | Nice-to-have | Convenient named poses, but an agent can replay absolute moves; create-once semantics and heavy read-validation add risk for modest value. |
| `obsbot_zoom_vendor` | Droppable | Duplicate zoom on an uncalibrated scale that provably disagrees with UVC; kept only for speed control. |
| `obsbot_gimbal_move_speed` | Droppable | Velocity jog is subsumed by absolute moves + aiming; already absent on Linux, and firmware over-range behavior makes it a foot-gun. |
| `obsbot_debug_probe` | Droppable | Explicitly RE/diagnostics-only, `--debug`-gated raw byte access — not part of the agent surface. |