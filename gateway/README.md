# gateway

Python "two-faced" MCP gateway for the **M5Stack 公式 [StackChan](https://docs.m5stack.com/ja/StackChan)** kit (custom [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) firmware in [`../firmware/main/boards/stackchan/`](../firmware/main/boards/stackchan/)).

```
┌─────────────┐  stdio MCP  ┌──────────────┐  WebSocket MCP  ┌──────────┐
│ MCP client  │ ──────────▶ │   gateway    │ ──────────────▶ │  ESP32   │
│ (Claude等)  │ ◀────────── │  (this dir)  │ ◀────────────── │ StackChan│
└─────────────┘             │              │                 └──────────┘
                            │  /capture    │ ◀─ HTTP POST ──┘  (JPEG)
                            └──────────────┘
```

The gateway exposes a clean stdio MCP server to the LLM client (left) while
speaking the xiaozhi-esp32 WebSocket MCP dialect to the device (right). It
also runs a small HTTP server (`/capture`) so the ESP32 can upload photos.

## Setup

```bash
cd gateway
cp .env.example .env       # then edit .env (see below)
uv sync
```

Edit `.env`:
- `STACKCHAN_TOKEN`: Bearer token for ESP32 auth (must match firmware setting)
- `VISION_HOST`: LAN IP of this machine, as seen from the ESP32
  (something like `192.168.x.y` on a typical home network — run `ifconfig`
  or `ip addr` to find it). Required for `take_photo`.

## Run

```bash
uv run python -m stackchan_mcp
```

Default ports:
- WebSocket (ESP32 -> gateway): `0.0.0.0:8765`
- HTTP capture (ESP32 -> gateway): `0.0.0.0:8766`

## Tests

```bash
uv run pytest tests/ -v
```

## Register as MCP server

### Claude Code (`~/.claude.json`)

```json
{
  "mcpServers": {
    "stackchan-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/stackchan-mcp/gateway",
        "python",
        "-m",
        "stackchan_mcp"
      ],
      "env": {
        "STACKCHAN_TOKEN": "your-secret-token-here",
        "VISION_HOST": "your.host.lan.ip"
      }
    }
  }
}
```

### Claude Desktop (`claude_desktop_config.json`)

Same shape, under `mcpServers`.

## Tools exposed to MCP client

| Tool | Description |
|---|---|
| `get_status` | Gateway connection state (ESP32 connected? device info?) |
| `get_device_info` | ESP32 device status (battery, volume, WiFi, etc.) |
| `take_photo(question?)` | Trigger camera capture; returns saved JPEG path |
| `set_volume(volume)` | Speaker volume 0-100 |
| `set_brightness(brightness)` | Screen brightness 0-100 |
| `move_head(yaw, pitch, speed?)` | Drive yaw + pitch servos |
| `get_head_angles` | Read current yaw + pitch servo angles |
| `get_touch_state` | Touch sensor state (press/release/stroke) |
| `set_avatar(face)` | Switch avatar expression (`idle` / `happy` / `thinking` / `sad` / `surprised` / `embarrassed`) |
| `set_blink(state)` | Blink animation on/off |
| `set_mouth(state)` | Mouth shape (`closed` / `half` / `open` / `e` / `u`) |
| `check_vm_en` | Read PY32 VM EN GPIO state (servo power supply diagnostic) |

The mapping from these names to ESP32-side `self.*` MCP tools is in
`stackchan_mcp/stdio_server.py`.

## Architecture

```
stackchan_mcp/
├── __main__.py         # entry: starts gateway + stdio server
├── gateway.py          # singleton orchestrator
├── stdio_server.py     # MCP client side (stdio MCP server)
├── esp32_client.py     # ESP32 side (WebSocket MCP client + auth)
├── capture_server.py   # HTTP /capture endpoint for photo uploads
├── server.py           # legacy local WS test server (unused in prod)
├── mcp_router.py       # legacy local stub router (unused in prod)
├── protocol.py         # JSON-RPC 2.0 message helpers
├── tools.py            # ESP32-side tool definitions (stub/test)
├── audio_stream.py     # placeholder for future Opus pipeline
└── handlers/
    ├── robot.py        # legacy stubs
    ├── camera.py       # legacy stubs
    └── audio.py        # legacy stubs
```

Captures land in `~/.stackchan/captures/` by default.

## Manual smoke test (Python)

```python
import asyncio, json, websockets

async def smoke():
    async with websockets.connect(
        "ws://localhost:8765",
        additional_headers={"Authorization": "Bearer your-secret-token-here"},
    ) as ws:
        await ws.send(json.dumps({
            "type": "hello", "version": 1, "audio_params": {},
        }))
        print(await ws.recv())

        await ws.send(json.dumps({"type": "mcp", "payload": {
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        }}))
        print(await ws.recv())

        await ws.send(json.dumps({"type": "mcp", "payload": {
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }}))
        print(await ws.recv())

asyncio.run(smoke())
```

## Phase roadmap

- **Phase 1** (done): stdio MCP shell, ESP32 WebSocket bridge, tool routing
- **Phase 2** (done): real servo / volume / brightness via ESP32
- **Phase 3** (done): camera capture (JPEG over HTTP)
- **Phase 4** (planned): Opus audio stream (STT/TTS pipeline)
