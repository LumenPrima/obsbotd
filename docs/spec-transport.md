# OBSBOT Transport & Device Management Specification — Linux

Derived from the JS layer (`dist/transport/*.js`, `dist/device/*.js`). The native helper binary (`native/linux/helper.c`, prebuilt at `native/prebuilt/linux-<arch>/obsbot-helper`) is the other half of the contract; everything the JS reveals about its internals is captured here. macOS and Windows: identical stdio JSON-RPC protocol driving `obsbot-helper` / `obsbot-helper.exe`; not further specified.

---

## 1. Helper process & wire protocol

### 1.1 Process lifecycle

- Binary path: `<package root>/native/prebuilt/${platform}-${arch}/obsbot-helper` (package root = two directories up from `dist/transport/`). Unsupported platform → throw `` `transport not yet implemented for ${platform}` ``.
- Spawned with `stdio: ["pipe", "pipe", "inherit"]` — stdin/stdout are the RPC channel, **stderr passes through** (helper diagnostics must go to stderr, never stdout).
- Protocol: **newline-delimited JSON**, one request object per line on stdin, one response object per line on stdout.
- **Correlation is positional (FIFO)** — there are no request IDs. The client keeps a queue of waiters; each valid response line resolves the oldest waiter. Consequences the port must preserve:
  - A stdout line that is not valid JSON is **ignored without shifting the queue** (stray log line guard).
  - Valid JSON lacking a boolean `ok` field is **not a response**: it is dispatched as a push event (see 1.4) or ignored. Events must therefore never carry `ok`.
  - On a per-request **timeout**, the queue slot becomes a **tombstone**: it stays in the queue and silently swallows the eventual late reply. Removing it would hand that reply to the next waiter and desync every later call.
- Timeouts: `DEFAULT_RPC_TIMEOUT_MS = 10_000` for every op except snapshot; snapshot uses `SNAPSHOT_RPC_TIMEOUT_MS = 30_000` **plus** the caller's `settleMs` (`30_000 + (settleMs ?? 0)`). Rationale (verbatim intent): the budget exists to break a *wedge* (driver-level stall in a USB control transfer), not to police latency; a wedged helper stays alive and silent, so only a timeout settles the request. A timeout fails **only that request**, never the helper.
- Timeout error text (API surface — the consumer is an LLM):
  `helper request "<op>" timed out after <N>ms — the camera did not respond. Retry this call; if it repeats, check that no other application is using the camera.`
- Helper death: `exit` and `error` process events fail **all** pending requests at once (`failAll`) and mark the client permanently dead; every subsequent request rejects with the stored error. Messages:
  - exit: `camera link lost: the helper process exited (code <code|null>, signal <signal|none>). The connection resets automatically — retry this call.`
  - spawn/run error: `camera link lost: the helper process failed to run (<msg>). Retry this call; if it repeats, the helper binary may be missing or blocked.`
  - stdin stream error (EPIPE guard — an unhandled `error` on stdin would crash the host): `helper stdin error: <msg>`
  - `close()`: marks dead **first** with `helper process closed`, then closes the readline, ends stdin, kills the child. (Helpers also self-terminate on stdin EOF.)
- `isDead` — true once dead/closed. `deviceLost` — true once any op's error text matched a device-lost signature (see 1.3) while the process stayed alive (the unplug case). Both mean "binding unusable, must re-bind".

### 1.2 Response envelope

Every response: `{ "ok": true, ...op-specific fields }` or `{ "ok": false, "error": "<string>", ...optional flags }`. On `ok:false` the client throws `Error(resp.error ?? "helper error (no message)")` — **after** first testing the message against the device-lost signatures and setting the `deviceLost` flag if matched (flag set *before* throwing so it is recorded even when the caller swallows the error).

### 1.3 Device-lost error signatures (deliberately narrow)

A helper stays **alive** when its camera is unplugged — only the USB handle dies — so process death cannot detect unplug. These regexes, matched against op error strings, are the only unplug signal. Anything not matching leaves the binding alone (condemning on arbitrary errors would drop a healthy camera over one bad argument):

| Platform | Pattern | Meaning |
|---|---|---|
| linux | `/No such device/i` | helper formats errno via `strerror()`; ENODEV is exactly `"No such device"` |
| darwin | `/0xe00002c0/i` | kIOReturnNoDevice (hardware-observed 2026-07-21) |
| win32 | `/0x800701b1/i` | HRESULT_FROM_WIN32(433) ERROR_DEV_NOT_EXIST; measured, not inferred — covers both KsProperty and IAMCameraControl paths |

### 1.4 Unsolicited push events

The helper pushes hotplug events on stdout as JSON lines **without** an `ok` field:

```json
{"event": "camera_arrived", "path": "<string>", "name": "<string>"}
{"event": "camera_departed", "path": "<string>", "name": "<string>"}
```

Client behavior: any `event` value other than those two is ignored; missing `path`/`name` default to `""`; listener exceptions are swallowed (a throwing listener must not take down the stdout reader). Events are delivered **per process**, and a helper that has never run `enumerate` delivers nothing — see §3.5 (priming).

---

## 2. JSON-RPC op catalogue

All requests are `{"op": "<name>", ...params}`. `hex` fields are lowercase hex strings of raw bytes.

| Op | Request params | Success response fields | Notes |
|---|---|---|---|
| `version` | — | `version: string` | |
| `enumerate` | — | `devices: [{path, name, locationId?, vid?, pid?}]` | On Linux `path` is a `/dev/videoN` node; **Linux helper does not report `vid`/`pid`** (paths carry no USB identity); `locationId` is macOS-only. Client coerces: `path`/`name` via `String(x ?? "")`, `locationId`/`vid`/`pid` kept only if `typeof === "number"`. Running `enumerate` is also what activates event delivery (§3.5). |
| `open` | `path: string` | `xuNode: number` | Opens the device at `path`, **unconditionally releasing whatever device the helper previously held** (`doOpen` semantics — re-opening a different candidate on one helper looks like a physical device swap). Return `xuNode < 0` ⇒ node opened but exposes **no UVC Extension Unit** (e.g. the Tiny 2's metadata/ISP `/dev/video` node) — not a usable candidate. |
| `xu_set` | `selector: number`, `hex: string` | — | UVC XU `SET_CUR` on the vendor Extension Unit at the given selector, payload = decoded hex bytes. (Linux: `UVCIOC_CTRL_QUERY` on the XU node discovered by `open`; the XU **unit number is discovered by the helper, not exposed in the JS**.) |
| `xu_get` | `selector: number`, `length: number` | `hex: string` | UVC XU `GET_CUR`, reading `length` bytes; client returns `Buffer.from(hex, "hex")`. |
| `zoom_range` | — | `min: number`, `max: number` | Zoom bounds in device units (V4L2 zoom-absolute query; exact CID not revealed in JS). |
| `zoom_set` | `units: number` | — | Absolute zoom in device units. |
| `snapshot` | `path?: string`, `maxDim?: number`, `quality?: number`, `settleMs?: number` | `base64: string`, `width: number`, `height: number`, `mime: string`, `sourceFormat?: string`; failure may add `busy: true` | Captures one real frame after the settle delay. `busy: true` on failure ⇒ another app holds the capture pin → client throws `CameraBusyError`. **Known helper defects the client patches around (§6): the frame is returned unvalidated (may be truncated MJPEG) and `maxDim` is ignored (always 1920×1080).** `settleMs` is caller-supplied, capped at 15000 by the tool schema. |
| `camctrl_set` | `property: number`, `value: number`, `flags: number` | — | CameraControl-style property write. Properties 0/1 = pan/tilt → V4L2 `V4L2_CID_PAN_ABSOLUTE`/`V4L2_CID_TILT_ABSOLUTE`, value in **arc-seconds**. `flags = 2` (manual/absolute) is what the JS passes on the pan/tilt fallback path. |
| `camctrl_get` | `property: number` | `value: number`, `flags: number` | For 0/1 reads the same V4L2 controls — **last-commanded value, not live** (see §2.1). |
| `camctrl_range` | `property: number` | `min: number`, `max: number` | For 0/1: arc-second bounds (hardware: pan ±468000, tilt ±324000, step 3600). |
| `pantilt_set` | `pan: number`, `tilt: number` | — | **Linux-only.** Both axes (arc-seconds) committed in a **single `VIDIOC_S_EXT_CTRLS`** — implemented by `v4l2_set_pantilt()` in `native/linux/helper.c`. Not a convenience wrapper; see §2.1 for why one ioctl is load-bearing. An **older helper** answers with an error containing `unknown op` — the client detects `/unknown op/i` and falls back (§2.1). |
| `procamp_set` | `property: number`, `value: number`, `flags: number` | — | VideoProcAmp-style write (brightness etc. — property numbering owned by the codec/tools layer). |
| `procamp_range` | `property: number` | `min: number`, `max: number` | (No `procamp_get` op is used by this client.) |

Error strings are free-form helper text; the only strings the client keys behavior on are the device-lost signatures (§1.3), `busy: true` on snapshot, and `/unknown op/i` for the `pantilt_set` fallback.

### 2.1 V4L2/UVC semantics the JS documents (must-know for the port)

**Pan/tilt units and CT mapping.** `V4L2_CID_PAN_ABSOLUTE`/`V4L2_CID_TILT_ABSOLUTE` map directly to the UVC `CT_PANTILT_ABSOLUTE` control (Camera Terminal, **selector 0x0D**), whose unit is **arc-seconds** per both UVC and V4L2 specs. Hardware-confirmed: pan range ±468000 / tilt ±324000, step 3600 — exactly ±130°/±90° at 3600 units/degree. **Historical bug to not repeat:** an earlier version divided by 1000 ("millidegrees"), wrong by 3.6× on both read and write — *self-consistently* wrong, so a move-then-read check reported 0 error while the true physical angle was ~28% of what was asked.

**Non-volatile control caching.** `VIDIOC_QUERY_EXT_CTRL` reports pan/tilt as non-volatile on this kernel, so V4L2 core serves its own cache instead of re-querying the device: `camctrl_get` for pan/tilt returns the **last-commanded** value. A genuinely live reading would need a raw USB read with uvcvideo briefly detached, which conflicts with concurrent video capture and was deliberately dropped. Gimbal moves on Linux are **open-loop**.

**Why `pantilt_set` must be a single ioctl.** The two axes are ONE UVC control (`CT_PANTILT_ABSOLUTE`, 8 bytes) that uvcvideo exposes as two V4L2 controls. A write naming a single axis read-modify-writes the other from a source chosen by the device's `GET_INFO` bits; when that source is a live `GET_CUR`, it is sampled while the first axis is still travelling and commits that axis back to where it started — the move is silently half-cancelled. Two parallel single-axis sets hit exactly this (measured whenever uvcvideo probed the camera asleep and pan/tilt kept `UVC_CTRL_FLAG_AUTO_UPDATE`; writeup: UVCVIDEO-LINUX-POSITION-2026-07-21.md §4.1, §9). One `VIDIOC_S_EXT_CTRLS` carrying both CIDs makes the hazard **unreachable** on stock kernels.

**Fallback:** if `pantilt_set` errors with `/unknown op/i` (older helper binary), issue `Promise.all([camctrl_set(0, pan, 2), camctrl_set(1, tilt, 2)])` — degraded (carries the cancellation risk) but still moves the gimbal. Any other error propagates.

---

## 3. Device discovery, binding, hotplug (DeviceManager)

### 3.1 Candidate filtering

```
REMO_VID = 0x3564            // registered to Remo Inc. (OBSBOT's manufacturer)
OBSBOT_MODEL_PIDS = { 0x3564: {0xfef8 /* Tiny 2 */} }
OBSBOT_NAME_RE = /obsbot/i
```

- Remo ships non-OBSBOT devices under the same VID, so candidacy must gate on **VID + known-model PID**, never VID alone — *except on Linux*, where the helper reports no vid/pid, so the gate falls back to the name regex against `device.name`. (On Windows/macOS the strict gate correctly excludes software sources like the "OBSBOT Virtual Camera" DirectShow filter, which matches the regex and poisoned multi-candidate binding.)
- **Metadata-node filtering happens at open time, not enumerate time**: the Tiny 2 exposes multiple `/dev/video` nodes; a node that opens but returns `xuNode < 0` has no XU unit (metadata/ISP node) and is rejected with reason `"<path>: opened but has no XU unit"`.

### 3.2 Registry model

- Serial-keyed `Map<serial, {helper, transport, locationId, path, name}>` — **identity is the camera serial** (read via `readSerial()` on a freshly-opened device); `locationId` is display-only, never binding truth. One `HelperProcess` per bound camera; nothing spawns until `get()`/`listCameras()` needs it.
- **Scratch scan helper**: one helper reused across scan attempts (each `open` releases the previous device). A winning candidate's scratch helper is **promoted** into the registry (no extra spawn; `scanHelper` set to `undefined` — ownership transfers). A losing scan keeps the scratch helper cached for the next scan, unless condemned (below). A dead cached scratch helper is discarded on next use (it would otherwise be a corpse every scan talks to).
- USB open is exclusive; per multi-camera spec §4.3, **any** open failure during a scan = "not mine, skip" (exclusive-access is not distinguishable from other failures at this layer).

### 3.3 `bind(wantSerial?)` scan algorithm

1. `enumerate` on the scratch helper; filter with `isObsbotCamera`.
2. For each candidate, in order: `open(path)` (failure → record `"<path>: open failed: <msg>"`, continue); `xuNode < 0` → record `"<path>: opened but has no XU unit"`, continue; construct platform transport over the scratch helper; `readSerial()` (failure → record `"<path>: <msg>"`, continue). Record serial in `found` map; remember last success as `matched`. With `wantSerial`, **stop at first match**; without it, probe **every** candidate (a partial scan could under-report ambiguity or pick the wrong "only" camera).
3. Outcomes:
   - `wantSerial` matched → promote + `ensureWatcher()` → return `{transport, serial}`; otherwise throw `UnknownCameraError(wantSerial, [...found serials])` — message `` `unknown camera "<serial>"; available: <list | "(none)">` ``.
   - No serials found → **unconditionally discard the scratch helper** (close + forget), then `ensureWatcher()`, then throw. Message: `no OBSBOT camera found` if nothing was rejected, else `` `no OBSBOT camera found — <n> candidate(s) rejected: <reason; reason; …>` `` (the rejection reasons distinguish "no camera" from "camera present but wouldn't answer"). The discard is unconditional **even when the bus looks empty**: a stale helper process can under-report the bus as empty (measured on macOS/AVFoundation — empty path for 2+ minutes until a fresh process saw the device instantly), so "empty" is not evidence of health. Cost: one spawn (~60ms) per failed bind, nothing on the happy path.
   - More than one distinct serial → `AmbiguousCameraError([...serials])` — message `` `multiple cameras attached; specify one of: <list>` ``.
   - Exactly one → promote + `ensureWatcher()` → return.

### 3.4 `get(serial?)` — the resolver every tool call uses

1. `pruneDeadEntries()` first (below).
2. With `serial`: registry hit → return its transport; miss → `bind(serial)`.
3. Without: exactly one bound → return it ("once bound, stay bound"); more than one → `AmbiguousCameraError`; zero → `bind()`.

**`pruneDeadEntries()`**: drop every registry entry whose helper `isDead` **or** `deviceLost` — and **close before deleting** (a device-lost helper still runs and still holds the USB device; merely forgetting it leaks the process and blocks the replacement from ever opening the camera — the first cut of the fix did exactly that). Deliberately a boolean check, not a liveness probe (no round trip on the hot path); a wedged-but-alive helper is deliberately *not* condemned here — that's the per-request timeout's job.

**Readiness gating & self-heal (referenced contract):** `ensureReady()` (tools layer) self-heals on a throw via invalidate → re-bind → fresh helper; the readiness gate drains `takeReconnected()` to surface `reconnected: true` on the next command. `takeReconnected(serial?)`: with a serial — was that serial re-bound since last asked (clear-on-read); without — true if *any* serial is pending-reconnected, draining all. `everBound` (serials ever bound) is kept **separate** from the registry so it survives `invalidate()`; a `promote()` of a serial already in `everBound` is a re-bind and lands it in `reconnectedSerials`.

**`invalidate(serial?)`**: drop one (or all) registry entries, best-effort-closing each helper first; next `get()` re-scans fresh. **`shutdown()`**: best-effort close of all registry helpers + scratch helper + watcher in parallel, then clear.

### 3.5 Hotplug watcher & priming

- `helperFactory(getMgr, make?)` wraps helper creation so **every** helper (scratch, registry, watcher) subscribes `onCameraArrived`/`onCameraDeparted` **before** `start()` (an arrival during spawn must not be lost). Handlers are wrapped with `.catch(() => {})` — an unhandled rejection inside a stdout line handler would kill the process. `getMgr` is a thunk because the manager is constructed with the factory.
- **Watcher**: one long-lived helper kept alive purely to receive bus events, spawned only once `everBound.size > 0` (never grab-on-sight — the camera is shared with Zoom/OBS/OBSBOT Center; the watcher opens nothing and holds no exclusive handle). Needed because in the bound steady state the registry helper is the *only* live subscriber, and a departure closes it — the paired arrival would be delivered to nobody.
- **Priming is not optional**: a helper that has never run `enumerate` receives no events (measured three-arm experiment, both macOS and Windows: primed-present → events yes; never-primed → no; primed-during-absence → macOS yes, Windows **no** — Windows drops events whose path is absent from a per-process `g_knownPaths` cache that only an enumerate-while-present fills). `ensureWatcher()` therefore: (a) respawn watcher if missing/dead, (b) call `enumerate()` on it every time (re-priming is Windows insurance; one-time activation suffices on macOS). **Never fatal** — it runs after `promote()`, so a throw would report an exception while the camera is actually bound; failures are logged to stderr and the next call retries.
- **`handleCameraDeparted(e)`**: for every registry entry with `entry.path === e.path`: best-effort close the helper (it still holds the device), delete the entry. Then `ensureWatcher()` (the close may have removed the last live subscriber before the paired arrival). Without this, `obsbot_devices` reports a phantom `bound` entry — serial and all — for a camera sitting on the desk.
- **`handleCameraArrived(_e)`** — self-heal only, never first-bind: return immediately if `everBound` is empty (never ours), if anything is bound already, or if a ladder is already running (`rebinding` flag — two ladders would race two binds into `promote()`). Otherwise run the **arrival re-bind ladder**: delays `[0, 400, 1200, 3000]` ms (≈4.5 s total, first immediate). Before each attempt, sleep the delay and re-check the registry (a tool call may have bound first). Each attempt calls `bind()`. Sizing rationale (hardware, 2026-07-21): for many seconds after re-enumeration the Tiny 2's vendor mailbox is intermittently not-ready (reply slot reads back with magic byte zeroed); measured 22 failures in 80 attempts in the first 14 s post-replug vs 0/120 in steady state — one attempt is ~a 1-in-4 coin flip. Logging (stderr, **never** stdout — that's the RPC channel): each failed attempt `obsbot-mcp: arrival re-bind attempt <i>/<n> failed: <msg>`; success on attempt >1 `obsbot-mcp: arrival re-bind succeeded on attempt <i>`; exhaustion `obsbot-mcp: arrival re-bind gave up after <n> attempts; the next tool call will bind normally`. Silent on a clean first attempt. Never throws.

### 3.6 `listCameras()` / `list()`

- `list()`: raw `enumerate()` pass-through on the scratch helper (unfiltered, no serials) — used by snapshot's virtual/NDI source lookup only.
- `listCameras()`: `pruneDeadEntries()` first (else a stale entry reports `bound` for an unplugged camera). Report registry entries as `{serial, locationId, name, status: "bound"}` **without re-opening** (avoids self-conflict with our own held handle). Then enumerate + filter; skip candidates whose `path` (cross-platform dedup key) or `locationId` matches a bound entry. For the rest: open → skip if `xuNode < 0` → `readSerial` → skip duplicate serials → `{serial, locationId, name, status: "available"}`. Any failure → `{locationId, name, status: "busy", reason}` — enumerable but not identifiable, reported **without** a serial rather than omitted.

---

## 4. Vendor frames end to end: sendVendor / recvVendor / recvStatus / readSerial

XU selector constants (camera-side UVC Extension Unit constants, identical on all OSes):

| Constant | Value | Purpose |
|---|---|---|
| `VENDOR_XU_SELECTOR` | `0x02` | vendor SET_CUR frame injection **and** the per-command reply mailbox |
| `RESPONSE_SELECTOR` | `0x02` | per-command reply read-back (unproven path — reads back zeros; per the 2026-07-19 hardware sweep, selector 6 returns the status block, not a reply, and preset read-back lives on flat selectors 12/13) |
| `STATUS_SELECTOR` | `0x06` | 60-byte status block |
| `DEFAULT_REPLY_LEN` | `60` | reply mailbox read size |
| `STATUS_BLOCK_LEN` | `60` | status block read size |

- **`sendVendor(frame)`** → `xu_set(0x02, frame)`. Fire-and-forget.
- **`recvVendor(frame, length=60)`** → `xu_set(0x02, frame)` then `xu_get(0x02, length)` (single unvalidated read — the unproven path above).
- **`recvStatus(length=60)`** → `xu_get(0x06, length)`. No preceding SET — reading selector 6 *is* the trigger; it returns the current 60-byte status block.
- **Sequence numbers (`nextSeq`)**: per-transport counter starting at 0; `seq = seq >= 0xffff ? 1 : seq + 1; return seq` — first value 1, wraps 0xFFFF → 1, **never 0**. Consumed by `buildFrame(seq)` for every vendor frame (gimbalSpeed sends, readSerial).

**`readSerialVia(transport)`** (shared by all three platforms so the polling/validation logic exists once):

```
UG_GET_SN_CMD = 0x18c8   // wire cmd of UG_GET_SN
REPLY_LEN     = 60
POLL_ATTEMPTS = 8
POLL_DELAY_MS = 30
```

1. `seq = nextSeq()`; build header-only `UG_GET_SN` GET frame (`encodeVendorGet("UG_GET_SN").buildFrame(seq)`); `xu_set(0x02, frame)`.
2. Poll up to 8 times: **sleep 30 ms first, then** `xu_get(0x02, 60)`. The delay-before-read is load-bearing: the reply lands tens of ms after the request; zero-delay polling drains all 8 attempts in ~7 ms and reads only the stale previous frame → spurious "no reply". Hardware-verified 2026-07-20: 0 ms fails every rapid read, 30 ms is reliable. (Unit tests with a synchronous fake never exercise this latency — that's how the bug shipped.)
3. The mailbox retains the *previous* reply until the new one lands, so a reply is trusted only when it: parses cleanly via `parseFrame` (magic + header CRC + payload CRC), has `cmd === 0x18c8`, echoes **this call's** `seq`, and has non-empty payload → return `decodeSerial(payload)`. Parse failures/mismatches → keep polling.
4. Exhaustion: throw with diagnostics naming what the mailbox actually held (dedupe raw reads by hex to report `unchanged` vs `<n> distinct`):
   `readSerial: no valid UG_GET_SN reply — 8 reads, <churn>; mailbox <description>` where description is one of:
   - `was our own request echoed back with the magic byte zeroed` — matched when `raw.length === req.length && raw[0] === 0x00 && req[0] === 0xaa && raw[1..] === req[1..]` (the signature of the 2026-07-21 post-replug outage; magic byte is `0xAA`);
   - `` `held cmd 0x<hex> seq <n>[, empty payload] (wanted cmd 0x18c8 seq <n>)` `` if the last read parses;
   - `` `unparseable: <parse error>; first bytes <first 8 bytes hex>` `` otherwise.

---

## 5. Transport public API (the contract the tools layer consumes)

`transport.js` itself exports only `CameraBusyError` (`name = "CameraBusyError"`, default message `"camera in use by another application"`) — thrown when a snapshot fails because another app holds the capture pin. The transport contract (implemented by `LinuxTransport(helper)`):

| Method | Semantics on Linux |
|---|---|
| `sendVendor(frame: Buffer)` | XU SET_CUR selector 0x02 |
| `recvVendor(frame, length=60): Buffer` | SET on 0x02, then GET 0x02 (unproven — reads zeros) |
| `recvStatus(length=60): Buffer` | GET selector 0x06, status block |
| `xuRaw(selector, data)` / `xuGetRaw(selector, length)` | raw XU SET/GET passthrough (used by `readSerialVia`) |
| `zoomRange(): {min, max}` / `zoomSet(units)` | helper `zoom_range` / `zoom_set`, device units, no conversion |
| `snapshot(opts): {mime, width, height, base64, sourceFormat?}` | helper `snapshot` + local patches (§6); throws `CameraBusyError` on `busy` |
| `camCtrlSet(property, value, flags)` | helper `camctrl_set` passthrough (no unit conversion on the set path — callers using it for pan/tilt pass arc-seconds) |
| `camCtrlRange(property): {min, max}` | helper `camctrl_range`; for property 0 or 1 convert asec → degrees with `Math.round(x / 3600)` on both min and max (rounding is fine here: advertised bounds, not a live pose, no arithmetic accumulates on them) |
| `camCtrlGet(property): {value, flags}` | helper `camctrl_get`; for property 0 or 1 convert `value = value / 3600` **as a float, NOT rounded**. Device GET_RES is 3600 asec = 1°, firmware emits whole degrees only — rounding recovers no hardware precision; what not-rounding preserves is Linux-specific: uvcvideo echoes back the value `gimbalSet` last wrote (`round(deg * 3600)`), so a fractional *commanded* pose (e.g. aimAtPixel's composed target) survives the round trip. Last-commanded value, not live (§2.1). |
| `procAmpSet(property, value, flags)` / `procAmpRange(property)` | helper `procamp_set` / `procamp_range` passthrough |
| `gimbalSet(yawDeg, pitchDeg, _rollDeg)` | `panTiltAbsolute(round(yawDeg * 3600), round(-pitchDeg * 3600))`. Roll ignored. Sign conventions: V4L2 pan + = camera-left = library +yaw (no negation); V4L2 tilt + = up, library +pitch = down (**negate pitch**). Hardware-verified to physically move the gimbal (2026-07-21). |
| `panTiltAbsolute(panAsec, tiltAsec)` | `pantilt_set` (single ioctl), falling back on `/unknown op/i` to two parallel `camCtrlSet(0/1, x, 2)` — §2.1 |
| `gimbalSpeed(yaw, pitch, roll, autoStopMs)` | vendor `AI_SET_GIM_SPEED` frame via `encodePtzMoveSpeed(-yaw, pitch, roll).buildFrame(nextSeq())` — **yaw negated**: firmware velocity-yaw is inverted relative to position-yaw, so negating makes +yaw pan camera-left for both move-speed and move-angle. If `autoStopMs > 0`: sleep that long, then send `encodePtzMoveSpeed(0,0,0)` with a fresh seq. Fire-and-forget, no readback. **Hidden from the Linux tool surface** (no live position feedback to confirm a burst stays in mechanical range); implemented only for interface conformance. |
| `gimbalRecenter()` | `panTiltAbsolute(0, 0)` — same single-ioctl path (two axes moving at once is exactly the shape the read-modify-write hazard cancels) |
| `readSerial(): string` | `readSerialVia(this)` — §4 |
| `nextSeq(): number` | 1‥0xFFFF wrapping counter — §4 |
| `close()` | `helper.close()` |

---

## 6. LOCAL PATCH behavior in the snapshot path (required for the port)

Context: the native helper returns MJPEG frames **unvalidated**, and the Tiny 2 Lite sporadically emits truncated frames, especially at stream start; the helper also **ignores `maxDim`** (always returns 1920×1080). Both defects are corrected client-side and the port must reproduce this.

### 6.1 `isCompleteJpeg(buf) → boolean`

A truncated MJPEG frame has a start-of-image marker but no end-of-image marker (the camera padded/cut the transfer); trailing NUL padding is legal.

1. If `buf.length < 4` or `buf[0] !== 0xFF` or `buf[1] !== 0xD8` → false.
2. `end = buf.length`; while `end > 2 && buf[end-1] === 0x00`: `end--` (strip trailing NULs).
3. Return `buf[end-2] === 0xFF && buf[end-1] === 0xD9`.

### 6.2 `maybeScaleJpeg(buf, width, height, maxDim, quality) → {buf, width, height} | null`

- Return `null` (meaning: send the full-size frame unchanged) if `maxDim` is falsy, `width`/`height` falsy, or `max(width, height) <= maxDim`.
- `scale = maxDim / max(width, height)`
- Target dims, forced even, minimum 2: `tw = max(2, round((width * scale) / 2) * 2)`, same for `th`.
- Quality map, JPEG 1–100 → mjpeg qscale 31–2 (lower is better): `q = max(2, min(31, round(31 - (quality / 100) * 29)))`.
- Run synchronously: `ffmpeg -hide_banner -loglevel error -f mjpeg -i pipe:0 -vf scale=<tw>:<th> -frames:v 1 -q:v <q> -f mjpeg pipe:1`, stdin = `buf`, output buffer cap 32 MiB, timeout 10 000 ms (~50 ms typical for 1080p→640).
- Accept only if exit status 0, stdout non-empty, **and** `isCompleteJpeg(stdout)` → `{buf: stdout, width: tw, height: th}`. Any failure, missing ffmpeg, or exception → `null` — worse tokens, never a lost image.

### 6.3 Snapshot retry loop

Request built from opts (`path`, `maxDim`, `quality`, `settleMs` included only when defined). Up to **4 attempts**; each attempt is a fresh `snapshot` RPC with timeout `30_000 + (settleMs ?? 0)` ms:

1. `resp.ok === false`: if `resp.busy` → throw `CameraBusyError(resp.error ?? undefined)`; else throw `Error(resp.error ?? "snapshot failed")`. **No retry on RPC-level failure** — retries are only for truncated frames.
2. Decode `resp.base64`. If `!isCompleteJpeg(buf)`: set `lastErr = "truncated MJPEG frame from camera (<len> bytes)"` and retry (a retry costs one frame, ~150 ms).
3. Complete frame: apply `maybeScaleJpeg(buf, resp.width, resp.height, opts.maxDim, opts.quality ?? 80)`; if it returns a result, substitute buf/width/height.
4. Return `{mime: resp.mime, width, height, base64, sourceFormat?}` — `sourceFormat` included only when it is a non-empty string.

After 4 truncated frames (`lastErr` initial value `"snapshot failed"`), throw:
`` `<lastErr> — 4 consecutive frames were truncated. Retry; if it persists, check USB bandwidth (other apps streaming?).` ``