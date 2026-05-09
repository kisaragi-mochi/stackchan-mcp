# Architecture

Target hardware: **M5Stack official [StackChan](https://docs.m5stack.com/ja/StackChan)** kit (2025 Kickstarter shipping version). The diagrams below describe how this repo's gateway and firmware bridge an MCP client to that kit.

## Component overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│ MCP client (e.g. Claude Code, Claude Desktop)                           │
│                                                                         │
│  - Speaks standard stdio MCP                                            │
│  - Sees a clean tool surface: get_status, take_photo, move_head, ...    │
└─────────────────────────────────────────────────────────────────────────┘
                              │ stdio MCP (JSON-RPC)
                              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ gateway (Python, this repo's gateway/)                                  │
│                                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                   │
│  │ stdio MCP    │  │ WebSocket    │  │ HTTP capture │                   │
│  │ server       │  │ server (8765)│  │ server (8766)│                   │
│  │ (LLM side)   │  │ (ESP32 side) │  │ (photo POST) │                   │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘                   │
│         │                 │                 │                           │
│         └────► gateway.py (singleton orchestrator) ◄─────────────────── │
└─────────────────────────────────────────────────────────────────────────┘
                              │ WebSocket MCP + HTTP POST
                              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ ESP32 (M5Stack CoreS3 + StackChan)                                      │
│                                                                         │
│  - xiaozhi-esp32 firmware with custom stackchan board                   │
│  - WebSocket MCP server: self.robot.*, self.camera.*, self.touch.*, ... │
│  - HTTP POST: camera.take_photo() → multipart/form-data → /capture      │
└─────────────────────────────────────────────────────────────────────────┘
```

## Tool name mapping

The gateway translates between two tool naming conventions:

| MCP client side (clean) | ESP32 side (xiaozhi-esp32) |
|---|---|
| `get_status` | (handled locally, no ESP32 call) |
| `get_device_info` | `self.get_device_status` |
| `take_photo` | `self.camera.take_photo` |
| `set_volume` | `self.audio_speaker.set_volume` |
| `set_brightness` | `self.screen.set_brightness` |
| `move_head` | `self.robot.set_head_angles` |
| `get_touch_state` | `self.touch.get_touch_state` |
| `set_avatar` | `self.avatar.set_face` |
| `set_blink` | `self.avatar.set_blink` |
| `set_mouth` | `self.avatar.set_mouth` |
| `check_vm_en` | `self.robot.check_vm_en` |
| `set_led` | `self.led.set_color` |
| `set_all_leds` | `self.led.set_all` |
| `set_leds` | `self.led.set_many` |
| `clear_leds` | `self.led.clear` |

The mapping lives in `gateway/stackchan_mcp/stdio_server.py`.

## Photo flow

1. MCP client calls `take_photo({"question": "what do you see?"})`.
2. Gateway forwards to ESP32 as `self.camera.take_photo`.
3. ESP32 captures JPEG, POSTs to gateway's HTTP `/capture` (port 8766),
   multipart/form-data with `question` and `file` fields.
4. Gateway saves JPEG to `~/.stackchan/captures/capture_<ms>.jpg`,
   returns the file path to the MCP client.
5. MCP client reads the file directly.

The ESP32 needs to know the gateway's LAN IP to POST. Set `VISION_HOST` in
`gateway/.env` to the LAN IP of the host running the gateway.

## Auth

ESP32 connects to the gateway WebSocket with `Authorization: Bearer <token>`.
The token is set on both sides:
- Gateway: `STACKCHAN_TOKEN` (or legacy `BEARER_TOKEN`) in `gateway/.env`
- ESP32: configured via the xiaozhi-esp32 WiFi setup UI on first boot

## Connection lifecycle

The ESP32 is the WebSocket client and the gateway listens on port `8765`. If
the gateway restarts, the existing socket drops. Firmware treats that as an
audio-channel close, returns to idle, and retries the WebSocket connection in
the background with bounded backoff from 5 seconds up to 60 seconds.

Intentional device-side closes, such as ending a listening session, do not
schedule this reconnect loop.

## Phase roadmap

- **Phase 0**: stdio MCP shell, ESP32 WebSocket bridge, tool routing → done
- **Phase 1**: real servo, volume, brightness → done
- **Phase 2**: HTTP camera capture → done
- **Phase 3**: avatar, blink, mouth, touch (Si12T), gesture (TAP/STROKE) → done
- **Phase 4**: 12x WS2812C base RGB LEDs via PY32 IO expander → done
- **Phase 5** (planned): Opus audio stream (STT / TTS pipeline)
