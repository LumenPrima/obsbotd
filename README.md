# obsbotd

MCP daemon for the OBSBOT Tiny 2 Lite camera. A ground-up Python replacement
for the `obsbot-mcp` npm package: no native helper binary, no stdio-per-client
servers, no single-owner IPC election — one daemon owns the camera and every
agent connects to it over streamable-http.

```
MCP clients (Claude Code, hermes, LibreChat, ...)
        │  streamable-http  http://127.0.0.1:8626/mcp
        ▼
     obsbotd  ──►  V4L2/UVC ioctls (ctypes)   gimbal, zoom, focus, status
              ──►  vendor XU protocol         wake/sleep, AI tracking, exposure
              ──►  ffmpeg                     snapshots (validated), clips, preview
```

## Design decisions

- **No persistent device handle.** Every tool call re-discovers `/dev/video*`
  (sub-millisecond). Plugging/unplugging the camera needs no event handling:
  an absent camera yields `{"ok": false, "camera_present": false, ...}` as a
  normal result, and the next call after a replug just works.
- **Frame validation at capture.** The camera sporadically emits truncated
  MJPEG frames; each snapshot is a stream-copied raw frame checked for a
  complete SOI..EOI, retried up to 4x, then optionally downscaled (default
  640px — snapshot pixels are LLM tokens).
- **Auto-wake.** A sleeping camera streams garbage with its lens stowed
  face-down; snapshots and moves wake it first.
- **Atomic pan+tilt.** Both axes go in one `VIDIOC_S_EXT_CTRLS` ioctl —
  uvcvideo's per-control read-modify-write otherwise cancels half the move.
- **Honest readback.** uvcvideo caches `CT_PANTILT_ABSOLUTE`, so
  `obsbot_gimbal_position` is labeled last-commanded, not live.
- **Measured optics.** `obsbot_aim_at_pixel` / `obsbot_zoom_to_fit` use the
  intrinsics-solved FOV model (67° wide HFOV, 0.957 vertical correction,
  magnification = 3·ratio − 2) and composed-rotation aiming inherited from
  the old package's calibration work (`docs/spec-tools.md` §4).

The complete reverse-engineered protocol lives in `docs/spec-*.md`; the wire
layer is golden-tested against the JS codec's exact output (`tests/`).

## Tools (18)

`obsbot_snapshot` · `obsbot_status` · `obsbot_wake` · `obsbot_sleep` ·
`obsbot_gimbal_move` · `obsbot_gimbal_recenter` · `obsbot_gimbal_position` ·
`obsbot_zoom` · `obsbot_aim_at_pixel` · `obsbot_zoom_to_fit` ·
`obsbot_ai_track` · `obsbot_ai_track_speed` · `obsbot_focus` ·
`obsbot_exposure` · `obsbot_image_adjust` · `obsbot_image_mode` ·
`obsbot_record_clip` · `obsbot_preview`

Dropped from the old 36: presets (create-once risk, little value), vendor
zoom (uncalibrated duplicate), velocity jog (footgun, was hidden on Linux
anyway), debug probe, capture sessions (clips are synchronous now), devices
(single camera; `obsbot_status` covers presence).

## Run

```sh
uv venv && uv pip install -e .          # once
.venv/bin/python -m obsbotd.server      # listens on 127.0.0.1:8626
```

As a service:

```sh
cp systemd/obsbotd.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now obsbotd
```

Client config (any MCP client): `{"type": "http", "url": "http://127.0.0.1:8626/mcp"}`.
Non-MCP agents can drive it with bare HTTP POSTs — see `docs/AGENT-GUIDE.md`
for the full integration guide, tool reference, and usage rules.

## Tests

`tests/` are hardware-free (protocol golden vectors + aim geometry).
`.venv/bin/python -m pytest tests/ -q`

Known environment limits: mic capture and the preview window need the user to
own the seat's device ACLs (`/dev/snd`, display) — over SSH the clip records
silent and preview reports why.
