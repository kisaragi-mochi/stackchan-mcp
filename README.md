**English** | [日本語](README.ja.md)

# stackchan-mcp

An MCP (Model Context Protocol) bridge for the **M5Stack official [StackChan](https://docs.m5stack.com/ja/StackChan)** (2025 Kickstarter shipping kit), letting any LLM client drive the device.

> Born out of the [stack-chan project](https://github.com/mongonta0716/stack-chan) community (originated by Takawo-san). This repository targets the M5Stack official StackChan kit that grew out of that lineage.

```
┌─────────────┐     stdio MCP      ┌──────────────┐    WebSocket MCP    ┌──────────────┐
│ MCP client  │ ─────────────────▶ │   gateway    │ ──────────────────▶ │ ESP32 (CoreS3│
│ (e.g.Claude)│ ◀───────────────── │  (Python)    │ ◀────────────────── │  +StackChan) │
└─────────────┘                    │              │                     └──────────────┘
                                   │  /capture    │ ◀── HTTP POST (JPEG) ──┘
                                   └──────────────┘
```

From any MCP client (Claude Code / Claude Desktop / others) you can call StackChan operations such as head movement, camera capture, touch sensor reads, and avatar expression switches.

## Repository layout

This repository is a monorepo.

| Directory | Contents |
|---|---|
| `firmware/` | Full git subtree of [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32). The custom StackChan board lives at `firmware/main/boards/stackchan/`. |
| `gateway/` | Python MCP gateway. stdio MCP server (LLM side) + WebSocket MCP client (ESP32 side) + HTTP capture server. |
| `docs/` | [`architecture.md`](docs/architecture.md): full component diagram, tool name mapping, photo flow, auth, phase roadmap. [`firmware-sync.md`](docs/firmware-sync.md): upstream xiaozhi-esp32 sync playbook. [`remote-access.md`](docs/remote-access.md): Tailscale Funnel setup for non-LAN use. |

## Target hardware

**M5Stack official [StackChan kit](https://docs.m5stack.com/ja/StackChan)** (Kickstarter 2025 shipping version). The firmware in this repository is meant to replace the kit's [factory firmware](https://docs.m5stack.com/ja/StackChan#%E5%87%BA%E8%8D%B7%E6%99%82%E3%83%95%E3%82%A1%E3%83%BC%E3%83%A0%E3%82%A6%E3%82%A7%E3%82%A2).

| Part | Spec |
|---|---|
| **Body** | M5Stack CoreS3 (ESP32-S3, 16MB Flash, 8MB PSRAM) |
| **Neck servos** | SCS0009 ×2 (yaw + pitch, serial bus, TX=GPIO6, RX=GPIO7) |
| **Camera** | GC0308 (DVP, 320×240) |
| **Touch** | FT6336 / Si12T |
| **Display** | ILI9342 (SPI, 320×240) |

> A self-built stack-chan (Takawo-san's original design) may also work as long as the pin assignments and I2C addresses match. Reports and PRs welcome.

## Tools (callable by MCP clients via the gateway)

| Tool | Description | Status |
|---|---|---|
| `get_status` | Gateway connection state | ✅ |
| `get_device_info` | ESP32 device state (battery / volume / WiFi / etc.) | ✅ |
| `take_photo(question?)` | Capture a frame, save as JPEG, return the path | ✅ |
| `set_volume(volume)` | Speaker volume (0-100) | ✅ |
| `set_brightness(brightness)` | Screen brightness (0-100) | ✅ |
| `move_head(yaw, pitch, speed?)` | Move the neck (servos) | ✅ |
| `get_touch_state` | Touch sensor state (press / release / stroke / etc.) | ✅ |
| `set_avatar(face)` | Switch avatar expression (`idle` / `happy` / `thinking` / `sad` / `surprised` / `embarrassed`), or `off` to hide the avatar and disable blink so the underlying WiFi config / OTA / settings screens are visible. Any other face brings the avatar back and restores blink. | ✅ |
| `set_blink(state)` | Blink on/off | ✅ |
| `set_mouth(state)` | Mouth open/close (one-shot, held until next call) | ✅ |
| `set_mouth_sequence(steps)` | Queue and play a list of `{shape, duration_ms}` steps locally for TTS lip-sync — no per-step WebSocket RTT jitter | ✅ |
| `check_vm_en` | Check servo power supply (VM EN HIGH) state | ✅ |

See `gateway/README.md` for full schemas.

## Quick start

### 1. Flash the firmware (CoreS3)

```bash
cd firmware
docker run --rm -v $PWD:/project -w /project espressif/idf:v5.5.2 \
  python ./scripts/release.py stackchan
# → releases/v2.2.6_stackchan.zip

# Flash (after USB-connecting the CoreS3)
esptool.py --chip esp32s3 --port /dev/cu.usbmodem1101 -b 460800 \
  write_flash 0x0 build/merged-binary.bin
```

WiFi configuration happens after the ESP32 boots — connect from a smartphone to its setup UI (the xiaozhi-esp32 standard flow).

### Configuring the WebSocket gateway URL and auth token

The firmware reads these NVS keys for the gateway connection:

- `websocket.url` — the gateway WebSocket URL (e.g. `ws://192.168.1.100:8765/`)
- `websocket.fallback_url` — optional second gateway URL to try when
  `websocket.url` cannot be reached or does not complete the server hello flow
- `websocket.token` — the bearer token sent as `Authorization: Bearer <token>`,
  matched against `STACKCHAN_TOKEN` / `BEARER_TOKEN` on the gateway side
  (leave both empty to skip authentication entirely)

There are three practical ways to provide them:

1. **Build-time defaults via Kconfig (recommended for developers)**: run
   `idf.py menuconfig` → `Component config` → `Xiaozhi Assistant`, and set:
   - `Default WebSocket gateway URL (fallback when NVS is empty)` →
     `CONFIG_DEFAULT_WEBSOCKET_URL` (e.g. `ws://192.168.1.100:8765/`)
   - `Fallback WebSocket gateway URL` →
     `CONFIG_DEFAULT_WEBSOCKET_FALLBACK_URL`
   - `Default WebSocket auth token (fallback when NVS is empty)` →
     `CONFIG_DEFAULT_WEBSOCKET_TOKEN` (leave empty if your gateway accepts
     unauthenticated connections)

   By default these only apply when the corresponding NVS key is empty.
   For first-time flashes onto a fresh device this is exactly what you want.
   If both a primary and fallback URL are configured, the firmware tries them
   in deterministic order and keeps the first candidate that completes the
   WebSocket server hello flow.

2. **Use the on-device WiFi config UI (recommended for fresh users)**: while
   the device is in WiFi configuration mode, open the captive portal at
   `http://192.168.4.1`, switch to the **Advanced** tab, and fill in:
   - **WebSocket Gateway URL** (e.g. `ws://<gateway-host>:8765/`) — the
     primary gateway candidate.
   - **Fallback Gateway URL** (e.g. `wss://<node>.<tailnet>.ts.net/`) —
     optional second candidate, tried only after the primary candidate
     fails the server-hello flow.
   - **Gateway Token** — optional bearer token, sent as
     `Authorization: Bearer <token>` to both candidates when set. The
     current value is never displayed (the WiFi config AP is
     unauthenticated, so the GET endpoint reports only whether a token
     is configured). Leave the field blank to keep the existing token,
     type a new value to update it, or hit ❌ to fall back to the
     build-time `CONFIG_DEFAULT_WEBSOCKET_TOKEN`. On stock builds where
     no Kconfig default is set this disables auth; on builds that ship
     a default token, ❌ reverts to that default rather than truly
     clearing authentication. To switch the device to an unauthenticated
     gateway on a build that ships a default token, rebuild the
     firmware with the Kconfig default empty (or set a non-empty token
     on the gateway side that matches the build default).

   Submit to persist the values to the `websocket` NVS namespace
   (`websocket.url` / `websocket.fallback_url` / `websocket.token`); they
   are read on the next boot. This is the intended path for end users
   running a pre-built firmware. Clearing a URL field with the ❌ button
   and submitting again falls back to the matching
   `CONFIG_DEFAULT_WEBSOCKET_*` Kconfig value (or to "no fallback" when
   no Kconfig default is set).

3. **Write `websocket.url` / `websocket.fallback_url` / `websocket.token`
   directly to NVS** (advanced): for example with a custom NVS-write tool
   over serial. Same persistence semantics as the WiFi config UI;
   primarily useful for batch provisioning.

4. **Temporary source hardcode (not recommended)**: editing
   `websocket_protocol.cc` can unblock local experiments, but keep it out of
   commits.

Common gateway URL setups:

| Mode | Primary URL | Fallback URL |
| --- | --- | --- |
| Local only | `ws://<gateway-host>:8765/` | empty |
| Tailscale only | `wss://<node>.<tailnet>.ts.net/` | empty |
| Local with remote fallback | `ws://<gateway-host>:8765/` | `wss://<node>.<tailnet>.ts.net/` |

#### Existing devices with stale NVS — `CONFIG_FORCE_DEFAULT_WEBSOCKET_URL`

If you are flashing onto a device that previously ran upstream xiaozhi-esp32
firmware, NVS will already contain `websocket.url=wss://api.tenclass.net/...`
written by the upstream OTA-config path. In this case the empty-NVS fallback
in option 1 above will **not** trigger, and the device will keep trying to
talk to tenclass instead of your local gateway. There is currently no
runtime tool to clear the `websocket` NVS namespace selectively.

To work around this without erasing all of NVS (which would also drop WiFi
credentials), enable the force-override switch:

- `Force CONFIG_DEFAULT_WEBSOCKET_URL/TOKEN to override NVS` →
  `CONFIG_FORCE_DEFAULT_WEBSOCKET_URL=y`

When set, **non-empty** Kconfig URL/token values override whatever NVS holds.
Empty Kconfig values still fall through to the NVS-based behaviour, so leaving
the token Kconfig empty keeps any NVS-stored token in use. The boot log will
show `FORCE: overriding NVS websocket.url with Kconfig: NVS=... -> ...` so you
can verify the override fired. This switch is the recommended way to bring
ex-xiaozhi hardware onto a local stackchan-mcp gateway, and to lock CI/dev
images to a known gateway URL.

The switch is opt-in so end-user devices configured at runtime keep their
NVS-priority semantics.

#### Developer-local overrides — `sdkconfig.defaults.local`

For local hardware testing, do not put personal gateway URLs or tokens in the
tracked `firmware/sdkconfig.defaults`. Instead, create a gitignored local file:

```bash
cd firmware
cat > sdkconfig.defaults.local <<'EOF'
CONFIG_DEFAULT_WEBSOCKET_URL="ws://<your-lan-ip>:8765/"
CONFIG_DEFAULT_WEBSOCKET_FALLBACK_URL="wss://<node>.<tailnet>.ts.net/"
CONFIG_DEFAULT_WEBSOCKET_TOKEN="<your-dev-token>"
CONFIG_FORCE_DEFAULT_WEBSOCKET_URL=y
EOF
```

Both `python ./scripts/release.py <board>` and plain `idf.py build` will read
this file when it exists. The file is ignored by git, so personal settings
cannot be added accidentally with `git add -A`.

### 2. Start the gateway

The gateway can either be installed as the published PyPI package
(recommended for end users) or run from this repository as a checkout
(recommended for contributors who want to follow `main`).

#### Option A: install as a tool (recommended for end users)

For an isolated install that does not collide with your system Python or
other Python projects, use one of:

```bash
uv tool install stackchan-mcp
# or
pipx install stackchan-mcp
```

Then run the gateway:

```bash
stackchan-mcp
```

If you prefer a project-managed virtualenv, `pip install stackchan-mcp`
inside an active venv works as well, and `python -m stackchan_mcp`
inside that venv is equivalent to `stackchan-mcp`. Just avoid
`pip install` against the system Python (PEP 668).

The `STACKCHAN_TOKEN`, `VISION_HOST`, and other settings documented in
[`gateway/README.md`](gateway/README.md#setup) can be supplied via environment
variables, the active shell, or a `.env` file in the working directory.

#### Option B: from source via uv (contributors)

```bash
cd gateway
cp .env.example .env       # set STACKCHAN_TOKEN / VISION_HOST
uv sync
uv run python -m stackchan_mcp
```

If the gateway is restarted while the ESP32 is already connected, the firmware
automatically retries the WebSocket connection while idle. The retry delay starts
at 5 seconds and backs off up to 60 seconds; use `get_status` to confirm that
the device has reappeared. The same retry path also fires for any
post-handshake server-initiated close — gateway crashes, TLS-layer resets, and
gateway configurations that tear the WebSocket session down after the handshake
— so the device recovers automatically once the gateway accepts the next
connection attempt.

For non-LAN setups, see [`docs/remote-access.md`](docs/remote-access.md) for the
Tailscale Funnel flow and the `VISION_URL` capture callback setting.

### 3. Register as an MCP client (Claude Code example)

Add to `~/.claude.json`.

If you installed via `pip install stackchan-mcp`:

```json
{
  "mcpServers": {
    "stackchan-mcp": {
      "type": "stdio",
      "command": "stackchan-mcp",
      "env": {
        "STACKCHAN_TOKEN": "your-secret-token-here",
        "VISION_HOST": "your.host.lan.ip"
      }
    }
  }
}
```

If you installed from source via `uv`:

```json
{
  "mcpServers": {
    "stackchan-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run", "--directory", "/path/to/stackchan-mcp/gateway",
        "python", "-m", "stackchan_mcp"
      ]
    }
  }
}
```

See `gateway/README.md` for details.

## About the avatar images

`firmware/main/boards/stackchan/avatar_images.cc` is a **pure black RGB565 placeholder**. The firmware builds and runs, but the screen will display nothing.

For a personal avatar, keep PNG sources outside git and generate ignored local override files:

```bash
cd firmware
python scripts/avatar_convert/convert_avatars.py
```

By default, the converter reads PNGs from `~/.stackchan/avatar/` and writes:

- `firmware/main/boards/stackchan/avatar_images.local.cc`
- `firmware/main/boards/stackchan/avatar_images.local.h`

These local files are ignored by git. When `avatar_images.local.cc` exists, the StackChan firmware build uses it instead of the tracked black placeholder, so `git pull` will not overwrite your personal avatar.

The tracked `avatar_images.cc` / `avatar_images.h` files are public placeholder files. Maintainers who intentionally need to refresh those tracked files can pass `--tracked`, but personal avatars should use the default local output path.

If you add a local avatar after you have already built the firmware once, remove `firmware/build/` and rebuild so CMake can pick up the new local override.

Symbol list (see `avatar_images.h`):
- Expressions (6): `avatar_idle`, `avatar_happy`, `avatar_thinking`, `avatar_sad`, `avatar_surprised`, `avatar_embarrassed`
- Eyes (3): `avatar_eyes_open`, `avatar_eyes_half`, `avatar_eyes_closed`
- Mouth (5): `avatar_mouth_closed`, `avatar_mouth_half`, `avatar_mouth_open`, `avatar_mouth_e`, `avatar_mouth_u`

Expected PNG filenames under `~/.stackchan/avatar/`:

- Expressions: `idle.png`, `happy.png`, `thinking.png`, `sad.png`, `surprised.png`, `embarrassed.png`
- Eyes: `eyes_open.png`, `eyes_half.png`, `eyes_closed.png`
- Mouth: `mouth_closed.png`, `mouth_half.png`, `mouth_open.png`, `mouth_e.png`, `mouth_u.png`

Do not commit personal PNGs, generated local avatar files, photos, or other user-specific assets.

## Known issues

- The servo bus may hang on large-angle abrupt reversals (e.g. +60° → -60°). A fix is in progress via Motion::update_task interpolation.
- The touch sensor (Si12T) occasionally drops tap events. Sensitivity register tuning has room to improve here.

## License

This repository is dual-licensed.

| Scope | License |
|---|---|
| All (`gateway/`, top-level, most of `firmware/`) | **MIT License** (see `LICENSE`) |
| **SCServo_lib-derived files** under `firmware/main/boards/stackchan/` (SCS.{cc,h}, SCSCL.{cc,h}, SCSerial.{cc,h}, INST.h, SCServo.h) | **GNU GPL-3.0** (see `firmware/main/boards/stackchan/SCServo_lib_LICENSE.txt`) |

This split exists because Feetech's SCServo SDK is distributed under GPL-3.0. The **firmware binary as a whole**, which statically links SCServo_lib, is therefore **effectively distributed under GPL-3.0**.

The `gateway/` runs as an independent Python process and only talks to the ESP32 over the network (WebSocket), so it stays usable and derivable under the **MIT License**.

### upstream

`firmware/` is taken in via git subtree from [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) (MIT) — specifically the [kisaragi-mochi/xiaozhi-esp32](https://github.com/kisaragi-mochi/xiaozhi-esp32) fork. See [`docs/firmware-sync.md`](docs/firmware-sync.md) for the upstream sync playbook. SCServo_lib is a firmware component ported from the official [stack-chan](https://github.com/mongonta0716/stack-chan) (Takawo-san) repository.

## Related projects

- [M5Stack official StackChan documentation](https://docs.m5stack.com/ja/StackChan) — official documentation for the target hardware (factory firmware / wiring / API reference / etc.)
- [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) — the base ESP32 LLM client firmware
- [stack-chan](https://github.com/mongonta0716/stack-chan) — the original StackChan project (Takawo-san)
- [Model Context Protocol](https://modelcontextprotocol.io) — the MCP protocol specification

## Contributing

Issues and PRs are welcome. We aim to provide something the StackChan community can use as-is.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the development flow.
