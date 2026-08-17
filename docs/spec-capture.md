# Capture & Process-Plumbing Specification (obsbot-mcp v0.6.3)

Derived from `dist/capture/ffmpeg-args.js`, `dist/capture/manager.js`, `dist/mcp/log-sink.js`, `dist/mcp/framing.js`, `dist/index.js`, and `package.json`.

---

## 1. ffmpeg / ffplay invocations

The capture layer is split into a pure-function module (`ffmpeg-args.js` — parses device listings and builds argv arrays, no side effects) and a stateful `CaptureManager` (`manager.js`) that spawns processes.

### 1.1 Binary availability check

Before any record/preview operation, both `ffmpeg` **and** `ffplay` must exist. Check: `spawnSync(name, ["-version"], { stdio: "ignore" })` — the binary is present iff the spawn produced no `error`. If either is missing, raise `FfmpegMissingError` with exactly this message:

> `Recording/preview needs ffmpeg and ffplay, which aren't installed. Install with: winget install Gyan.FFmpeg (Windows) / brew install ffmpeg (mac) / apt install ffmpeg (Linux).`

### 1.2 Device probing (one per platform, result cached for process lifetime)

| Platform | Command / mechanism | Output parsed from |
|---|---|---|
| Windows (default branch) | `ffmpeg -hide_banner -f dshow -list_devices true -i dummy` | stderr |
| macOS (`darwin`) | `ffmpeg -hide_banner -f avfoundation -list_devices true -i ""` | stderr |
| Linux (`linux`) | No ffmpeg call. Enumerate `/dev` entries matching `/^video\d+$/`, map to `/dev/videoN`, sort lexicographically; for each, read the card name from sysfs: `/sys/class/video4linux/video<N>/name` (trimmed; device skipped if read fails or name empty) | filesystem |

Probe spawns use `stdio: ["pipe", "pipe", "pipe"]`, accumulate stderr as a string, and resolve on the child's `close` event. A spawn `error` rejects with `CaptureError("failed to run ffmpeg for device probe: <message>")`. If `/dev` is unreadable on Linux, the result is silently `{ video: [], audio: [] }`.

**Result shapes** (this union is how downstream code distinguishes platforms):

- dshow: `{ video: string[], audio: string[] }`
- v4l2: `{ video: Array<{ path: string, card: string }>, audio: [] }` (Linux never yields audio devices — v4l2 audio is a separate ALSA device; audio resolution returns undefined and callers must warn)
- avfoundation: `{ video: Record<name, index>, audio: Record<name, index> }` (name → numeric device index)

**Parsers (exact regexes):**

- `parseDshowDevices(stderr)`: per line (split on `/\r?\n/`), match `/"(.+)" \((video|audio)\)\s*$/`; group 1 is the friendly name, group 2 routes it to the video or audio list. (dshow prints one line per device on stderr: `[dshow @ ..] "Friendly Name" (video)`.)
- `parseV4l2DeviceName(stderr)`: match `/card\s+:\s+(.+)/`, return trimmed group 1. (ffmpeg prints `card         : OBSBOT Tiny 2 ...` lines for v4l2 inputs. Note: present in the code but the manager actually uses sysfs, not this parser.)
- `parseAvfDevices(stderr)`: stateful line scan. A line matching `/AVFoundation video devices/i` switches section to video; `/AVFoundation audio devices/i` switches to audio; before either, lines are ignored. Within a section, match `/\[\s*(\d+)\]\s+(.+)/` → index = `parseInt(group1, 10)`, name = trimmed group 2.

### 1.3 Device-name resolution

`source` is one of three values: `"device"` (the physical camera), `"virtual"`, `"ndi"`.

**Video (dshow / v4l2 — `resolveVideoName(devices, source)`):** The v4l2 shape is detected by `"video" in devices && devices.video.length > 0 && typeof devices.video[0] !== "string"`.

| source | v4l2: match `card` against | v4l2 returns | dshow: match name against |
|---|---|---|---|
| `virtual` | `/OBSBOT Virtual Camera/i` | matching entry's `path` | `/OBSBOT Virtual Camera/i` |
| `ndi` | `/NDI Webcam/i` | matching entry's `path` | `/NDI Webcam/i` |
| `device` (default) | `/OBSBOT/i` | matching entry's `path` | `/OBSBOT Tiny 2/i` **and not** `/Virtual/i` |

**Video (avfoundation — `resolveAvfVideoName`):** same regexes over the name keys; `device` case is `/OBSBOT/i` and not `/Virtual/i`. Returns `{ name, index }`.

**Audio:** dshow — first name matching `/OBSBOT.*Mic/i`. avfoundation — first entry whose name matches `/OBSBOT.*Mic/i`, returns `{ name, index }`. v4l2 — always `undefined` (see above).

Unresolvable video → `CaptureError("no '<source>' video source found (is OBSBOT Center / NDI running?)")`. Audio requested but unresolvable → `CaptureError("OBSBOT microphone not found; retry with audio:false for a silent clip")`.

### 1.4 Record argv (`ffmpeg`)

Platform dispatch inside `buildRecordArgs` is by `videoName.startsWith("/dev/")` → v4l2, else dshow. macOS uses the separate `buildAvfRecordArgs`.

**Linux (v4l2):**

```
-hide_banner -loglevel warning
-f v4l2
-i <videoName>                       # e.g. /dev/video0
[-f alsa -i <audioName>]             # only if audioName present
-t <durationSec>
-c:v libx264 -pix_fmt yuv420p
[-c:a aac]                           # only if audioName present
-y <outputPath>
```

**Windows (dshow):**

```
-hide_banner -loglevel warning -f dshow
-i video=<videoName>:audio=<audioName>    # or just  video=<videoName>  without audio
-t <durationSec>
-c:v libx264 -pix_fmt yuv420p
[-c:a aac]
-y <outputPath>
```

**macOS (avfoundation):**

```
-hide_banner -loglevel warning -f avfoundation
-i <videoIndex>:<audioIndex>              # or just  <videoIndex>  without audio
-t <durationSec>
-c:v libx264 -pix_fmt yuv420p
[-c:a aac]                                # only if audioIndex !== undefined
-y <outputPath>
```

Flag purposes: `-hide_banner -loglevel warning` quiets output; `-t` caps duration; `-c:v libx264 -pix_fmt yuv420p` gives broadly playable H.264; `-c:a aac` only when audio is captured; `-y` overwrite-allow (safe because pre-existing explicit output paths are rejected earlier).

### 1.5 Preview argv (`ffplay`)

Format pinning applies **only** when `source` is `"device"` (`pinTiny2Format = (source ?? "device") === "device"`). The source comments explain why, verbatim:

> The preview is for a human to watch the gimbal move, so smooth motion is the point of it. Left to negotiate, dshow settles on 1080p30 — but the Tiny 2 advertises mjpeg 1920x1080 all the way to 60fps (`ffmpeg -list_options`, confirmed 2026-07-25, and the 60fps stream verified to actually sustain 60 rather than merely claim it). Pinning the codec is what makes 60 reachable: yuyv422 tops out at 30 for 1080p, so without -vcodec the negotiation can land on a format that cannot deliver it.
>
> Deliberately NOT here: `-fflags nobuffer` / `-flags low_delay` / `-framedrop`. Those were tried against an apparent half-second lag on 2026-07-25 and appeared to fix it, but re-running the untouched arguments showed them equally fast — the lag was CPU contention from a concurrent test suite, not these flags. Adding them would have been treating a symptom that no longer reproduced. `-framedrop` in particular is a real trade (drop frames under load instead of running permanently late) and should be a deliberate choice, not a leftover.
>
> SCOPE: that pin describes the Tiny 2's own capture pin, not video sources in general. Neither alternative feed offers mjpeg — OBSBOT Center's virtual camera advertises nv12/yuv420p/yuyv422, the NDI Webcam devices UYVY only — so pinning it there does not merely fail to buy 60fps, it fails to OPEN the device ("Could not set video options"; verified against both, 2026-07-25). Those get negotiated arguments instead: smooth motion is worth pinning for only where the pin is achievable.

**Linux (v4l2):**

```
-hide_banner -loglevel warning
-f v4l2
[-input_format mjpeg -video_size 1920x1080 -framerate 60]   # only when source == "device"
-i <videoName>
-window_title "OBSBOT preview"
```

**Windows (dshow):**

```
-hide_banner -loglevel warning -f dshow
[-framerate 60 -video_size 1920x1080 -vcodec mjpeg]         # only when source == "device"
-i video=<videoName>
-window_title "OBSBOT preview"
```

**macOS (avfoundation)** — never pinned:

```
-hide_banner -loglevel warning -f avfoundation
-i <videoIndex>
-window_title "OBSBOT preview"
```

### 1.6 Output file naming and location

- Default output path: `<homedir>/Videos/OBSBOT/<timestampName>` (via `os.homedir()` + `path.join`).
- `timestampName()`: take the clock's ISO-8601 string and transform: remove all `-` and `:` (`replace(/[-:]/g, "")`), replace `T` with `-`, strip from the first `.` to end (`replace(/\..*$/, "")`), then wrap as `obsbot-<compact>.mp4`. Example: `2026-08-17T14:03:22.123Z` → `obsbot-20260817-140322.mp4`.
- If the caller supplied an explicit `outputPath` and that file already exists → `CaptureError("output file already exists: <path>")`. (The default timestamped path is *not* pre-checked.)
- `mkdirSync(dirname(outputPath), { recursive: true })` before spawning.
- Open-ended recordings (no `durationSec` given) default to `OPEN_ENDED_CAP_SEC = 3600` seconds — a hard 1-hour cap baked into `-t`.

---

## 2. CaptureManager lifecycle

### 2.1 Construction and injectable deps

`new CaptureManager(deps = {})` — every dependency defaults to the real thing and exists for tests:

| Dep | Default |
|---|---|
| `spawn` | `node:child_process.spawn` |
| `clock` | `() => new Date().toISOString()` |
| `hasBinary` | `spawnSync(name, ["-version"])`-based check (§1.1) |
| `fs` | `{ existsSync, mkdirSync }` from `node:fs` |
| `platform` | `os.platform()` |
| `probeDevices` (`probeFn`) | undefined — real per-platform probe used |

State: `sessions: Map<id, { session, child }>`, cached `devices` (probe runs once, memoized), monotonic counter `seq` starting at 0.

### 2.2 Session objects

- IDs: `` `cap${++this.seq}` `` → `cap1`, `cap2`, … per-process monotonic, never reused.
- Record session fields: `{ id, kind: "record", pid, source, outputPath, durationSec, startedAtIso }`.
- Preview session fields: `{ id, kind: "preview", pid, source, startedAtIso }` (no outputPath/duration).
- `pid` is `child.pid ?? -1`.
- `startedAtIso` from the injected clock.

### 2.3 Spawning

- Record: `spawn("ffmpeg", args, { stdio: ["pipe", "ignore", "ignore"] })` — stdin kept as a pipe **specifically so a graceful stop can write `"q"` to it**; stdout/stderr discarded.
- Preview: `spawn("ffplay", args, { stdio: "ignore" })` — no channels at all.

### 2.4 Orphan prevention / self-cleanup

Immediately after every spawn:

```js
child.once("exit", () => this.sessions.delete(session.id));
child.on("error", () => this.sessions.delete(session.id));
```

So a recording that hits its `-t` cap, a preview window the human closes, or a spawn failure all remove themselves from the map — `list()` never reports dead sessions.

### 2.5 `stop(id)`

- Unknown id → `CaptureError("no such capture session: <id>")`.
- **Record** sessions get a graceful shutdown: write `"q"` to the child's stdin (ffmpeg's quit key, which finalizes the MP4 moov atom), wrapped in try/catch (`/* already gone */`), then race the child's `exit` event against a `GRACEFUL_STOP_MS = 5000` ms timer. If `exit` wins → `graceful: true`; if the timer wins → `child.kill()` (default SIGTERM) and `graceful: false`. A `done` boolean guards against double-resolution; the timer is cleared on graceful exit.
- **Preview** sessions are just `child.kill()`; `graceful` stays `true`.
- Session is deleted from the map, and the return value is `{ kind, outputPath, graceful }` (`outputPath` is `undefined` for previews).

### 2.6 `list()` and `stopAll()`

- `list()` returns the session objects (not the child handles) as an array.
- `stopAll()` iterates a **snapshot** (`Array.from(this.sessions.values())`) — because each kill's `exit` handler mutates the map — calls `child.kill()` on every child inside try/catch (ignore failures), then `sessions.clear()`. No graceful `"q"` here: this is the shutdown path, so recordings stopped this way may lack a finalized index. This is the hook the server calls at process teardown so no ffmpeg/ffplay child outlives the MCP server.

### 2.7 Error taxonomy

`CaptureError extends Error` (name `"CaptureError"`) is documented as an *expected operational failure* — "surfaced to the user as text, not thrown to the client". `FfmpegMissingError extends CaptureError` (name `"FfmpegMissingError"`).

---

## 3. Supporting modules (brief)

### 3.1 `mcp/log-sink.js` — `makeLogSink(path, console_)`

Builds the diagnostic log sink. If `path` is falsy, returns `console_` unchanged. Otherwise returns `(msg) => { console_(msg); try { appendFileSync(path, `${new Date().toISOString()} ${msg}\n`); } catch { /* swallowed */ } }` — every message goes to both the console sink and, timestamped, appended to the file; file-write failures (bad path, full disk, read-only mount) are silently ignored because "none of that is worth taking the server down for". Rationale from the source: the MCP server's stderr is a pipe to whatever launched it (Claude Code hands it a socket), so `console.error` alone is write-only in practice; setting **`OBSBOT_LOG_FILE`** appends the same lines somewhere greppable. stdout is never an option: it is the JSON-RPC channel.

### 3.2 `mcp/framing.js` — `verifyFraming(readAiMode, want, before, opts)`

Best-effort settle-verification poll after writing an AI-framing mode. Defaults: `DEFAULT_ATTEMPTS = 30`, `DEFAULT_INTERVAL_MS = 200` (30 × 200 ms = 6 s ceiling; an awake→awake framing switch was observed parked in the m=6 "unknown" transient for ~3–4 s, and a 2.4 s window produced flaky false negatives in the field). Loop: read `aiMode` each attempt (sleeping `intervalMs` before every attempt except the first); return `{ verified: aiMode, matched: true }` the moment it equals `want`; return `{ verified: aiMode, matched: false }` as soon as it is neither `"unknown"` (the transient) nor still equal to `before` (i.e., it settled on a different stable framing — polling longer won't help); otherwise keep polling until attempts are exhausted, then `{ verified: last, matched: false }`. The write already succeeded; this only reports where the device settled. `opts` may override `attempts`, `intervalMs`, and `sleep`.

---

## 4. `index.js` CLI surface

- Shebang `#!/usr/bin/env node`; exports `NAME = "obsbot-mcp"`.
- **Main-module guard**: compares `fileURLToPath(import.meta.url)` against `process.argv[1]`, but only after passing **both** through `realpathSync` (falling back to the raw string if resolution throws, e.g. argv[1] names no existing file). Reason, per the source: npm installs the `bin` entry as a POSIX symlink (`/usr/local/bin/obsbot-mcp -> ../lib/node_modules/obsbot-mcp/dist/index.js`) and node reports argv[1] as the invoking path, not its resolution — a raw string compare silently exited 0 on every global install and `npx` run on Linux/macOS; Windows hid the bug because npm's `.cmd` shim runs node against the real path.
- If main: `debug = process.argv.includes("--debug")`, then `startServer({ debug })`; a rejected promise is logged via `console.error` and the process exits with code 1.
- **Flags in these files**: only `--debug`.
- **Environment variables in these files**: none are read directly; `OBSBOT_LOG_FILE` is documented in `log-sink.js` as the variable that supplies the log-file path (the actual `process.env` read happens in `mcp/server.js`, outside this file set).

---

## 5. `package.json` highlights

| Field | Value |
|---|---|
| name / version | `obsbot-mcp` / `0.6.3` |
| description | "Cross-platform MCP server for OBSBOT Tiny 2 (UVC) camera control" |
| mcpName | `io.github.lxman/obsbot-mcp` |
| type | `module` (ESM) |
| bin | `{ "obsbot-mcp": "dist/index.js" }` |
| files | `dist/`, `native/prebuilt/` — shipped prebuilt native helper binaries, implying a compiled HID/UVC helper outside the JS layer |
| engines | node `>=18` |
| dependencies | `@modelcontextprotocol/sdk ^1.0.0` (MCP server/transport), `zod ^3.23.0` + `zod-to-json-schema ^3.23.0` (tool input schemas defined in zod, converted to JSON Schema for MCP tool listings) |
| scripts | `build` = `tsc`; `build:helper` builds the native helper; `prebuild`/`version` run `scripts/sync-version.mjs` (version stamped into `src/version.ts` and `server.json`); `test` = vitest; `integration` = `node scripts/integration/run.mjs` |
| allowScripts | `esbuild@0.21.5: true` |

Python-reimplementation notes: no runtime dependency does capture work — all capture is shelling out to `ffmpeg`/`ffplay`; the only platform inputs are `os.platform()`-equivalent dispatch (`linux` / `darwin` / else→Windows), `/dev` + sysfs on Linux, and ffmpeg's stderr device listings elsewhere.