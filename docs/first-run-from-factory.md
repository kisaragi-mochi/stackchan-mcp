# First run: factory StackChan → Cursor

English | [日本語](first-run-from-factory.ja.md)

This is the path if you have an official M5Stack StackChan kit still on
factory firmware, and you want it as an MCP device (Cursor, Claude Code,
or anything that speaks Streamable HTTP).

The [Quick start](../README.md#quick-start) in the root README is the
short version. This page records the steps that actually fail on a desk
the first time.

## What you will have at the end

- stackchan-mcp firmware on the robot
- A gateway on your computer at `ws://<your-lan-ip>:8765/`
- Cursor (or another MCP client) calling `set_avatar`, `move_head`, `say`
- Optional classic Stack-chan face via [`examples/classic-avatar/`](../examples/classic-avatar/)

## Safety

- Plug USB-C into the **base**, not the face. The face port can yank the
  neck while you flash.
- Do not force the neck by hand while the servos are powered.
- Pitch should stay in **5–85°**.
- The robot is **2.4 GHz Wi-Fi only**. 5 GHz will not join.

## 1. Unbind factory pairing

Factory XiaoZhi (StackChan World) and this firmware are not interchangeable.
Unbind **before** you flash.

On the robot: **Setup → Account unbinding**, then let it restart.

Or in the **StackChan World** app: that device’s settings → **Device unbinding**.

If you also paired on [xiaozhi.me](https://xiaozhi.me/), unbind there too.

Skip this only if you never finished factory setup.

## 2. Flash the firmware

1. Download `merged-binary.bin` from the latest
   [`firmware-v*`](https://github.com/kisaragi-mochi/stackchan-mcp/releases)
   release. A clean flash at `0x0` wipes Wi-Fi. That is expected.
2. Install `esptool` (`uv tool install esptool` or `pipx install esptool`).
3. Short-press power. If no serial port appears, hold **RST** (next to the
   microSD slot) for 3 seconds until the LED goes green, then release.
4. Flash:

```bash
esptool --chip esp32s3 --port /dev/cu.usbmodemXXXX -b 460800 \
  write_flash 0x0 merged-binary.bin
```

On Linux use `/dev/ttyACM0` or `/dev/ttyUSB0`. On Windows use `COMn`.

M5Burner remains the factory restore hatch: search **StackChan**, tick
**Only Official**, burn.

## 3. Start the gateway (before you finish device Wi-Fi)

Install and run the published gateway. Daemon mode is what Cursor and
scripts share:

```bash
uv tool install 'stackchan-mcp[tts]'
# English TTS without Docker (optional; needs ffmpeg + the extra):
uv tool install --force --with edge-tts 'stackchan-mcp[tts]'

export VISION_HOST=<your-computer-lan-ip>   # e.g. 192.168.0.169
stackchan-mcp serve --transport streamable-http --no-mdns
```

`--no-mdns` is recommended on macOS. The default mDNS advertiser has
exhausted Apple Wi-Fi (Skywalk) memory and kernel-panicked a Mac mini
while the robot hotspot was up. You then **must** set the gateway URL
by hand in step 4.

Check `http://127.0.0.1:8767/healthz` returns `{"ok":true}`.

Find your LAN IP with `ipconfig getifaddr en0` (macOS) or `ip addr`.

## 4. Join Wi-Fi from a phone, not the computer

Do not join the robot hotspot from the Mac or PC that runs the gateway.

1. After flash, the screen shows a **settings cog**. That is normal.
   The full face appears only after the gateway talks to the device.
2. On the **phone**, wait until the robot is in config mode (gear /
   “connect to hotspot”).
3. Join the `Xiaozhi-…` network. Ignore “no internet”.
4. Open `http://192.168.4.1` if the portal does not appear.
5. Enter your **2.4 GHz** home Wi-Fi.
6. Open **Advanced**.
7. Set **WebSocket Gateway URL** to:

   `ws://<your-computer-lan-ip>:8765/`

   Trailing slash included. Leave the token blank unless you set
   `STACKCHAN_TOKEN` on the gateway.
8. **Save does not reboot.** That is expected. Tap **RST** or
   short-press power and **do not touch the screen** while it boots.

A short press is a tap (under a second). Holding the left power button
for 6 seconds powers the robot **off**.

### If you missed Advanced and only have a dot face

Wi-Fi icon + correct clock means LAN works. A lone xiaozhi dot with
`esp32_connected: false` means the URL was never saved.

Re-enter config:

1. Hover a finger over the screen.
2. Tap **RST**.
3. The instant the backlight comes on — before the face, before the
   Wi-Fi icon — tap the screen once (under 0.5 s).

Too late = dot face again (idle) or a red listen LED. RST and tap sooner.

Then repeat the phone portal, **Advanced** URL, Save, RST without a
screen tap.

## 5. Confirm the link

```bash
curl -s http://127.0.0.1:8767/status
```

You want `"connected": true` and a non-zero `tools_count`.

The first connected face is often a small idle icon. `set_avatar` with
`idle` / `happy` / … switches the named expressions. Volume defaults
around 70; `set_volume` 90 is a comfortable desk level.

## 6. Point Cursor at the daemon

In `~/.cursor/mcp.json` (or project `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "stackchan-mcp": {
      "url": "http://127.0.0.1:8767/mcp"
    }
  }
}
```

If `STACKCHAN_TOKEN` is set, add
`"headers": { "Authorization": "Bearer <token>" }`.

Reload MCP. Then you can ask the agent to look, change face, or speak.

Do **not** also spawn `stackchan-mcp` as a stdio server in the same
config. The daemon owns the ESP32 socket.

## 7. Optional: English speech

VOICEVOX is the default engine and is Japanese. For English, install
the `[tts]` extra plus `edge-tts`, put `ffmpeg` on `PATH`, and:

```bash
export STACKCHAN_TTS_ENGINE=edge-tts
export STACKCHAN_EDGE_TTS_DEFAULT_VOICE=en-GB-SoniaNeural
```

Then `say` with text. A supported emoji in the text can still change
the face; you can also call `set_avatar` by name and skip emoji.

## 7b. Optional: English listening

`listen()` transcribes on this computer. Install the STT extra next to
TTS, default the language to English, and restart the daemon:

```bash
export STACKCHAN_LISTEN_LANGUAGE=en
# checkout gateway:
uv run --extra tts --extra stt-faster-whisper --with edge-tts \
  stackchan-mcp serve --transport streamable-http --no-mdns
```

The first call downloads the Whisper `base` model (~140 MB). Then from
Cursor: ask the agent to listen and reply. The tool has **no schema
default** for `language` — omit it so `STACKCHAN_LISTEN_LANGUAGE`
applies, or pass `language="ja"` for one Japanese utterance.

## 8. Optional: classic Stack-chan face

The firmware idle face is a small icon. The classic two-big-eyes look
is a 90-frame matrix. It lives in PSRAM, so a robot reboot drops it.

Build once, then point the gateway at the file so **every device
connect** reloads it (gateway start and later reconnects). When this
path is set, the same hook also turns blink back on (firmware starts
with blink off):

```bash
uv run --with pillow python examples/classic-avatar/make_classic.py
export STACKCHAN_AVATAR_SET_PATH="$PWD/examples/classic-avatar/classic-matrix.rgb565"
```

Mode is inferred from exact file size (`matrix` here). Raise
`STACKCHAN_AVATAR_SET_TIMEOUT` if Wi-Fi power save makes the 3.3 MB
fetch miss the default 180 s. If the size is not layered or matrix,
set `STACKCHAN_AVATAR_SET_MODE` explicitly — the gateway will not guess.

This env var is in the current checkout. The published PyPI gateway
gains it on the next release; until then run the gateway from this
tree, or load once with:

```bash
uv run --with pillow python examples/classic-avatar/load_classic.py
```

See [`examples/classic-avatar/README.md`](../examples/classic-avatar/README.md).

## Cursor hooks (optional)

User hooks can call the same HTTP MCP endpoint. Keep them **fail open**
so an offline robot never blocks the editor. This repo does not ship
hook files; they belong in `~/.cursor/hooks.json` on your machine.

Useful events:

- `sessionStart` → `set_avatar happy` + a slight look-up
- `afterShellExecution` on a failed test command → `set_avatar sad`

## Factory restore

M5Burner → StackChan → Only Official → Burn. Unbind xiaozhi.me first if
you used this firmware’s pairing path, then bind StackChan World again.
