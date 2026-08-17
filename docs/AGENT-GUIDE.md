# Using the OBSBOT camera from any agent

The camera on this machine is controlled through **obsbotd**, an MCP server at

```
http://127.0.0.1:8626/mcp        (localhost only, no auth)
```

It works with any MCP-capable framework, and also with **plain HTTP POSTs** —
no MCP library, no handshake, no session management, no special headers.

## Quickest possible photo (bare shell)

```sh
curl -s -X POST http://127.0.0.1:8626/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"obsbot_snapshot","arguments":{"resolution":640}}}' \
| python3 -c '
import json,sys,base64
r = json.load(sys.stdin)["result"]["content"]
img = next(c for c in r if c["type"]=="image")
open("photo.jpg","wb").write(base64.b64decode(img["data"]))
print(next(c["text"] for c in r if c["type"]=="text"))'
```

That wakes the camera if needed, validates the frame, and writes `photo.jpg`.

## Option A — MCP-native agent

Point your framework's MCP client at the URL with streamable-http transport:

```jsonc
// generic JSON config (Claude Code, most frameworks)
{ "mcpServers": { "obsbot": { "type": "http", "url": "http://127.0.0.1:8626/mcp" } } }
```

```yaml
# hermes-style YAML
mcp_servers:
  obsbot:
    url: http://127.0.0.1:8626/mcp
```

```python
# python mcp SDK (>=2.0)
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async with streamable_http_client("http://127.0.0.1:8626/mcp") as (read, write, *_):
    async with ClientSession(read, write) as s:
        await s.initialize()
        result = await s.call_tool("obsbot_snapshot", {"resolution": 640})
```

## Option B — bare HTTP (no MCP library)

Every request is one JSON-RPC 2.0 object POSTed to `/mcp` with
`Content-Type: application/json`. The server is stateless: each POST stands
alone. `initialize` is accepted but **not required**.

**List tools** (names, descriptions, and full JSON-Schema input schemas —
treat this as the authoritative parameter reference):

```json
{"jsonrpc":"2.0","id":1,"method":"tools/list"}
```

**Call a tool**:

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call",
 "params":{"name":"obsbot_gimbal_move","arguments":{"yaw":15,"pitch":-5}}}
```

**Response shape** — `result.content` is a list of blocks:

- `{"type":"text","text":"{...json...}"}` — every control tool returns one
  JSON text block, e.g. `{"ok": true, "yaw": 15.0, "pitch": -5.0, ...}`.
- `{"type":"image","data":"<base64 jpeg>","mimeType":"image/jpeg"}` —
  `obsbot_snapshot` returns an image block plus a text block with
  `{"width","height","attempts"}`.

Failures are **normal results, not protocol errors**: parse the text block
and check `ok`. `{"ok": false, "error": "..."}` explains what to do;
`{"ok": false, "camera_present": false, ...}` means the camera is unplugged.

## Tool reference

All parameters optional unless marked; defaults shown. `tools/list` gives the
machine-readable schemas.

| Tool | Arguments | Does |
|---|---|---|
| `obsbot_snapshot` | `resolution` 256–1920 = 640 (longest edge px), `quality` 1–100 = 80, `settle_ms` = 0 | Photo → image + `{width,height,attempts}`. Auto-wakes a sleeping camera. |
| `obsbot_status` | — | Presence, awake, AI mode, FOV/zoom, HDR, focus, last-commanded gimbal pose. |
| `obsbot_wake` / `obsbot_sleep` | — | Wake (un-stows gimbal, moves camera) / sleep (stows lens face-down). |
| `obsbot_gimbal_move` | **`yaw`**, **`pitch`** (degrees) | Absolute move. +yaw = camera pans LEFT, +pitch = tilts DOWN. Clamped ±150 / ±90. |
| `obsbot_gimbal_recenter` | — | Return to yaw 0, pitch 0. |
| `obsbot_gimbal_position` | — | Last-COMMANDED pose. Not a live reading — see rules below. |
| `obsbot_zoom` | **`ratio`** 1.0–2.0 | Zoom; waits up to 3 s for arrival (`settled` in result). |
| `obsbot_aim_at_pixel` | **`x`**, **`y`**, **`frame_width`**, **`frame_height`** | Point camera at a pixel of the MOST RECENT snapshot. |
| `obsbot_zoom_to_fit` | **`x`**, **`y`**, **`width`**, **`height`**, **`frame_width`**, **`frame_height`**, `margin` = 0.1 | Center + zoom onto a region (x,y = top-left corner). |
| `obsbot_ai_track` | **`enabled`**, `mode` = "normal" (normal/upper-body/close-up/headless/lower-body/group/whiteboard/desk/hand) | Subject tracking on/off. While on, the camera moves ITSELF. |
| `obsbot_ai_track_speed` | **`speed`** standard\|sport | Tracking responsiveness. |
| `obsbot_focus` | **`mode`** auto\|manual, `position` 0–100 = 50, `face_priority` | Focus control. |
| `obsbot_exposure` | **`mode`** auto\|manual, `level` 0–100 = 50, `face_priority` | Exposure control. |
| `obsbot_image_adjust` | **`control`** brightness\|contrast\|saturation\|hue\|sharpness, **`level`** 0–100 | One image slider. |
| `obsbot_image_mode` | `hdr`, `fov` wide\|medium\|narrow, `white_balance` auto\|manual, `wb_temperature_k` = 5000 | Only passed params change. |
| `obsbot_record_clip` | `duration_s` = 10 (max 60), `audio` = true | Blocking 1080p clip → `~/Videos/OBSBOT/`; silent + note if mic inaccessible. |
| `obsbot_preview` | **`action`** start\|stop | Desktop preview window. Holds the stream — snapshots fail until stopped. |

## Rules of engagement (read before driving the camera)

1. **The camera is fast — never add sleeps.** A snapshot needs no delay for a
   static scene. The ONLY wait you ever use is `settle_ms: 1200` on a
   snapshot taken right after a gimbal/zoom move, and it happens inside the
   call. Longer waits buy nothing.
2. **Verify motion visually, never by readback.** `obsbot_gimbal_position`
   echoes the last command (the kernel caches the control); it stays wrong
   after tracking, wake, or sleep moved the gimbal on its own. `ok: true`
   means "command sent", not "camera moved". Take a snapshot and look.
3. **The see→point loop**: `obsbot_snapshot` → find your target's pixel →
   `obsbot_aim_at_pixel` (or `obsbot_zoom_to_fit` for a region) →
   `obsbot_snapshot {settle_ms: 1200}` → confirm. Always compute pixel
   coordinates from the most recent frame.
4. **Refusal errors are instructions.** aim/fit refuse when the frame is
   stale (camera just woke, zoom mid-travel, AI tracking active) and the
   `error` text says exactly what to do — do that and retry. Don't work
   around a refusal.
5. **`camera_present: false` = unplugged.** A normal state, not a fault.
   Report it and stop; don't restart things or diagnose. The next call after
   a replug just works.
6. **AI tracking fights manual control.** Disable it
   (`obsbot_ai_track {enabled: false}`) before aiming or precise framing.
7. **Resolution costs tokens.** 640 (~11 KB) for looking; 1920 only for the
   final detail shot.
8. **A wedge is not broken hardware.** Commands return ok but nothing
   changes → `obsbot_wake` then `obsbot_gimbal_recenter`, then re-verify with
   a snapshot.

## Operator troubleshooting (human, not agent)

```sh
systemctl --user status obsbotd      # is the daemon up
systemctl --user restart obsbotd     # cure-all
journalctl --user -u obsbotd -n 50   # logs
```

Source and full protocol documentation: the obsbotd repository root
(`docs/spec-*.md` for the reverse-engineered wire protocol).
