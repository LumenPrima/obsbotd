# obsbotd

A Linux MCP daemon for OBSBOT Tiny-series cameras. One daemon owns the
camera; every agent — Claude Code, LibreChat, custom frameworks, or anything
that can POST JSON — connects to it over streamable-http.

```
MCP clients (any framework, or bare curl)
        │  streamable-http  http://127.0.0.1:8626/mcp
        ▼
     obsbotd  ──►  V4L2/UVC ioctls (ctypes)   gimbal, zoom, focus, status
              ──►  vendor XU protocol         wake/sleep, AI tracking, exposure
              ──►  ffmpeg                     snapshots (validated), clips, preview
```

## Why this exists

The upstream [obsbot-mcp](https://www.npmjs.com/package/obsbot-mcp) npm
package did the hard part — reverse-engineering the vendor protocol and
calibrating the optics — but its architecture (a stdio MCP server per agent
session, a prebuilt native helper binary, single-owner IPC election between
sessions) produced chronic operational pain on a multi-agent Linux machine:
stale owner processes serving weeks-old code, unvalidated truncated frames,
and a closed binary the bugs lived in.

obsbotd keeps the protocol knowledge and throws away the architecture:

- **One daemon, many clients.** No elections, no per-session processes, no
  stale owners. Runs as a systemd user service.
- **No native binary.** The V4L2/UVC layer is ~200 lines of ctypes; ioctl
  numbers are computed from struct sizes so a layout mistake fails loudly.
- **Hotplug is free.** No device handle is held between calls — every tool
  call re-discovers `/dev/video*` (sub-millisecond). Unplugging the camera
  yields `{"ok": false, "camera_present": false}` as a normal result; the
  next call after a replug just works.
- **Frames are validated.** The camera sporadically emits truncated MJPEG
  frames, worst right after wake. Every snapshot is a stream-copied raw frame
  checked for a complete SOI..EOI, retried up to 4x, then optionally
  downscaled (default 640 px — snapshot pixels are LLM tokens).
- **Honest errors.** Every failure is a structured `{ok: false, error}` an
  agent can act on — never an opaque tool exception. Refusal messages say
  what to do instead.
- **Atomic pan+tilt.** Both axes in one `VIDIOC_S_EXT_CTRLS` — uvcvideo's
  per-control read-modify-write otherwise cancels half the move.
- **Measured optics.** `obsbot_aim_at_pixel` / `obsbot_zoom_to_fit` do
  visual servoing with the intrinsics-solved FOV model (67° wide HFOV, 0.957
  vertical correction, magnification = 3·ratio − 2) and composed-rotation
  aiming inherited from upstream's calibration work.

## Hardware support

Developed and hardware-verified against an **OBSBOT Tiny 2 Lite** on Linux
(kernel uvcvideo). The Tiny 2 shares the vendor protocol and should work
unchanged; other OBSBOT models are untested. Single camera per host.

Known kernel limitation: uvcvideo caches `CT_PANTILT_ABSOLUTE`, so gimbal
readback is the last-*commanded* pose, not live — the tools are labeled
accordingly, and the intended workflow verifies pointing visually.

## Install

Requires Python ≥ 3.12, `ffmpeg` (and `ffplay` for the preview window), and
read/write access to the camera's `/dev/video*` node (the `video` group).

```sh
git clone https://github.com/LumenPrima/obsbotd ~/obsbotd
cd ~/obsbotd
uv venv && uv pip install -e .        # or: python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m obsbotd.server    # listens on 127.0.0.1:8626
```

As a service:

```sh
cp systemd/obsbotd.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now obsbotd
loginctl enable-linger $USER          # optional: run without an active login
```

`OBSBOTD_HOST` / `OBSBOTD_PORT` override the bind address. The default is
localhost-only and there is no authentication — do not bind it to a reachable
interface unless you understand that anyone who can reach the port can drive
the camera and take pictures.

## Connect an agent

MCP-native config (Claude Code, most frameworks):

```json
{ "mcpServers": { "obsbot": { "type": "http", "url": "http://127.0.0.1:8626/mcp" } } }
```

No MCP library? The server answers bare JSON-RPC POSTs with plain JSON — a
naive agent can drive it with `curl`. **[docs/AGENT-GUIDE.md](docs/AGENT-GUIDE.md)**
has the full integration guide, the 18-tool reference, and the rules of
engagement (no sleeps, verify motion visually, treat refusals as instructions).

## Tools (18)

`obsbot_snapshot` · `obsbot_status` · `obsbot_wake` · `obsbot_sleep` ·
`obsbot_gimbal_move` · `obsbot_gimbal_recenter` · `obsbot_gimbal_position` ·
`obsbot_zoom` · `obsbot_aim_at_pixel` · `obsbot_zoom_to_fit` ·
`obsbot_ai_track` · `obsbot_ai_track_speed` · `obsbot_focus` ·
`obsbot_exposure` · `obsbot_image_adjust` · `obsbot_image_mode` ·
`obsbot_record_clip` · `obsbot_preview`

## Protocol documentation

`docs/spec-*.md` is a complete written specification of the vendor wire
protocol (60-byte V3 frames, CRC-16/USB, opcode tables, the 60-byte status
block, selector map) and the measured optics — extracted from upstream's
source as the reimplementation reference, and useful on its own to anyone
driving these cameras. The wire layer is golden-tested against upstream's
exact frame bytes (`tests/`).

## Tests

`tests/` are hardware-free: `.venv/bin/python -m pytest tests/ -q`

## Credits

The vendor protocol reverse engineering, hardware quirk catalog, and optical
calibration all come from [obsbot-mcp](https://www.npmjs.com/package/obsbot-mcp)
(MIT, © 2026 Michael Jordan). obsbotd is an independent Linux-native
reimplementation of the serving architecture around that knowledge.

## License

MIT — see [LICENSE](LICENSE).
