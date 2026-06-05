# gateway

Python "two-faced" MCP gateway for the **M5Stack official [StackChan](https://docs.m5stack.com/ja/StackChan)** kit (custom [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) firmware in [`../firmware/main/boards/stackchan/`](../firmware/main/boards/stackchan/)).

```
┌─────────────┐  stdio MCP  ┌──────────────┐  WebSocket MCP  ┌──────────┐
│ MCP client  │ ──────────▶ │   gateway    │ ──────────────▶ │  ESP32   │
│ (Claude...) │ ◀────────── │  (this dir)  │ ◀────────────── │ StackChan│
└─────────────┘             │              │                 └──────────┘
                            │  /capture    │ ◀─ HTTP POST ──┘  (JPEG)
                            └──────────────┘
```

The gateway exposes a clean stdio MCP server to the LLM client (left) while
speaking the xiaozhi-esp32 WebSocket MCP dialect to the device (right). It
also runs a small HTTP server (`/capture`) so the ESP32 can upload photos.

The package name on PyPI, the installed CLI command, and the MCP server id
in your client config are all `stackchan-mcp`.

## Install (end users)

The gateway is published to PyPI as `stackchan-mcp`. For end users, install
it as an isolated CLI tool:

```bash
uv tool install stackchan-mcp
# or
pipx install stackchan-mcp
```

Then run:

```bash
stackchan-mcp
```

`stackchan-mcp` reads its configuration (`STACKCHAN_TOKEN`, `VISION_HOST`,
etc.) from environment variables or a `.env` file in the working directory.
See the [Setup](#setup) section below for the supported variables. For the
firmware side (WebSocket gateway URL, auth token, NVS configuration), see
[`../README.md`](../README.md#configuring-the-websocket-gateway-url-and-auth-token).

If you prefer a project-managed virtualenv, `pip install stackchan-mcp`
inside an active venv works as well, and `python -m stackchan_mcp` inside
that venv is equivalent to `stackchan-mcp`. Just avoid `pip install`
against the system Python (PEP 668).

## Setup

```bash
cd gateway
cp .env.example .env       # then edit .env (see below)
uv sync
```

Edit `.env`:
- `STACKCHAN_TOKEN`: Bearer token for ESP32 auth (must match firmware setting)
- `VISION_URL`: full public capture URL for remote access tunnels, such as
  `https://stackchan.example.ts.net:8443/capture`
- `VISION_TOKEN`: optional separate Bearer token for capture uploads; if empty,
  `STACKCHAN_TOKEN` is reused
- `VISION_HOST`: LAN IP of this machine, as seen from the ESP32
  (something like `192.168.x.y` on a typical home network — run `ifconfig`
  or `ip addr` to find it). Required for `take_photo` when `VISION_URL` is not
  set.

## Run

```bash
uv run python -m stackchan_mcp
```

Default ports:
- WebSocket (ESP32 -> gateway): `0.0.0.0:8765`
- HTTP capture (ESP32 -> gateway): `0.0.0.0:8766`

## Daemon mode (Phase B)

For multi-client setups, run one shared Streamable HTTP daemon instead of
letting each MCP client spawn its own stdio gateway:

```bash
uv run stackchan-mcp serve --transport streamable-http
```

The daemon exposes MCP at `http://127.0.0.1:8767/mcp` by default, keeps the
existing ESP32 WebSocket and capture listeners, and serializes ESP32-bound
tool calls through a bounded command queue. See
[`../docs/178-daemon-setup.md`](../docs/178-daemon-setup.md) for environment
variables, bearer-token rules, `MCP_HTTP_ALLOWED_HOSTS`, bind safety, and
migration notes.

The zero-subcommand stdio mode remains supported and unchanged for existing
client configs.

By default, the gateway advertises the WebSocket endpoint as
`_stackchan-mcp._tcp.local.` via mDNS/DNS-SD so fresh firmware can discover it
on the local LAN. Run `stackchan-mcp --no-mdns` to disable this advertisement.

For non-LAN setups, see [`../docs/remote-access.md`](../docs/remote-access.md)
for the Tailscale Funnel flow.

When you restart the gateway during development, an already-connected ESP32
will notice the dropped WebSocket and retry while idle. The retry delay starts
at 5 seconds and backs off up to 60 seconds. After the gateway is listening
again, check `get_status` from the stdio MCP side to confirm the device is back.

## Configuration changes

The gateway reads `.env` once at process start. Because the gateway runs as a
**stdio MCP server** by default, editing `.env` while it is connected to an MCP
client does not take effect on the running process — and killing that stdio
gateway process directly will not auto-restart it; the MCP client owns the
lifecycle. In daemon mode, restart the daemon process after changing `.env`.

After editing `.env` (for example to update `STACKCHAN_TOKEN`, `VISION_URL`,
or `VISION_TOKEN`):

1. Reconnect the MCP client. In Claude Code this is `/mcp` to reconnect, or a
   full Claude Code restart.
2. Confirm `mcp__stackchan-mcp__get_status` returns `connected: true` with the
   expected `tools_count`.
3. If the ESP32 was already connected with a stale auth credential, hard-reset
   the device (`esptool.py --before default_reset --after hard_reset chip_id`,
   or DTR/RTS toggle via pyserial) so it reconnects with the fresh
   configuration.

`STACKCHAN_TOKEN` takes precedence over the legacy `BEARER_TOKEN`; setting
either is enough, but if you have both, keep them aligned.

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
| `set_avatar(face)` | Switch avatar expression (`idle` / `happy` / `thinking` / `sad` / `surprised` / `embarrassed`), or `off` to hide the avatar and disable blink so the underlying xiaozhi-esp32 screens (WiFi config UI, OTA, settings) are visible. A subsequent `set_avatar(<other face>)` brings it back and restores blink. |
| `set_blink(state)` | Blink animation on/off |
| `set_mouth(state)` | Mouth shape (`closed` / `half` / `open` / `e` / `u`), one-shot, held until next call |
| `set_mouth_sequence(steps)` | Queue and play a list of `{shape, duration_ms}` steps locally for TTS lip-sync. The firmware walks the queue without per-step network RTT. Calling `set_mouth`, `set_avatar`, or this tool again interrupts the in-flight sequence; autonomous blink is paused while a sequence is playing. |
| `check_vm_en` | Read PY32 VM EN GPIO state (servo power supply diagnostic) |
| `set_led(index, r, g, b)` | Set one of the 12 base RGB LEDs by index (`0..11`); channels `0..255`. Updates immediately. |
| `set_all_leds(r, g, b)` | Set all 12 base RGB LEDs to the same color. Updates immediately. |
| `set_leds(colors)` | Batch-set the first N LEDs from a `[[r,g,b], ...]` array (1..12 entries). Single I2C burst + one latch — use this for animations / multi-color patterns instead of N individual `set_led` calls. Trailing LEDs (beyond `len(colors)`) keep their previous color. Validation is atomic: a malformed entry rejects the whole call without mutating any LED. |
| `clear_leds` | Turn all 12 base RGB LEDs off. |

The 12 base LEDs are 12× WS2812C wired to the PY32L020 IO expander
(expander pin 13, not an ESP32 GPIO), so all four LED tools share the
PY32 I2C bus with the servo-power and Si12T touch paths. If the PY32
init fails at boot, the LED tools degrade with `available=false`
instead of cascading errors.

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

## License

The gateway Python code is distributed under the **MIT License** (see
`LICENSE`). The Windows wheel (`*-win_amd64.whl`) additionally bundles
a native `opus.dll` built from upstream Opus source via vcpkg by the
publish workflow. That binary is distributed under the **BSD 3-clause
license + Xiph extension**; the full notice ships in every
distribution form (sdist, `py3-none-any` wheel, `win_amd64` wheel) as
`LICENSE-THIRD-PARTY`. Non-Windows wheels and the sdist do not contain
any binary subject to that license — they rely on a system `libopus`
provided by the OS package manager (e.g. `apt install libopus0`,
`brew install opus`). See `stackchan_mcp/_libs/SOURCES.md` (also
shipped in the wheel) for build provenance and the per-release
SHA256 logged by CI.

The parent monorepo's `firmware/` directory contains SCServo_lib code
under GPL-3.0, but those files live only inside
`firmware/main/boards/stackchan/` and never enter this package. The
gateway and firmware communicate only over WebSocket, so the GPL/MIT
boundary is preserved at the process level.
