# firmware/ AGENTS.md (stackchan-mcp)

ESP32 firmware (xiaozhi-esp32 based, stackchan board) developer guide covering **build, flash, device operations, and common troubleshooting**.

For board-specific behavior (SCS0009 servo, Si12T touch, WS lifecycle, license boundary), see `firmware/main/boards/stackchan/AGENTS.md`.

## 1. Hardware specification

| Component | Specification |
|---|---|
| MCU | M5Stack CoreS3 (ESP32-S3, 16 MB Flash, **8 MB Octal PSRAM**) |
| Neck servos | SCS0009 x2 (TX=GPIO6, RX=GPIO7, yaw_id=1, pitch_id=2) — details in `boards/stackchan/AGENTS.md` |
| Camera | GC0308 (DVP, 320x240) |
| Touch | FT6336 (LCD screen) / Si12T (head top, I2C 0x68) — distinction matters, details in `boards/stackchan/AGENTS.md` |
| Display | ILI9342 (SPI, 320x240) |

## 2. Host environment (reference)

- **ESP-IDF**: Docker `espressif/idf:v5.5.2` via OrbStack (or any Docker host). **No native `idf.py` — always use Docker**.
- **esptool**: Host-installed `esptool.py` (v5.2.0+)
- **pyserial**: Host Python with `import serial` available (for serial monitor)
- **USB serial**: `/dev/cu.usbmodem*` (macOS) or equivalent

## 3. Pre-testing checklist (6 items)

Before starting any device verification session, confirm **all paths are live**. Skipping this leads to compound failures (gateway not running → WS connection fails → ESP32 enters idle → auto sleep → USB CDC port disappears).

Run these checks in parallel:

```bash
# 1. USB serial connected
ls /dev/cu.usbmodem*

# 2. Gateway process listening (most common oversight)
lsof -i :8765 | grep LISTEN
pgrep -fl stackchan_mcp

# 3. Network path to gateway (if using remote access)
# Verify your chosen access method (Tailscale Funnel, LAN direct, etc.)

# 4. Docker runtime (if building firmware)
orbctl status    # or equivalent Docker host check

# 5. sdkconfig.defaults.local URL/token alignment
cat firmware/sdkconfig.defaults.local | head -5

# 6. Build artifact freshness
git log -1 --format="%h %s"
ls -lh firmware/build/xiaozhi.bin 2>&1
```

| Failed check | Resolution |
|---|---|
| (1) No USB | Connect USB cable + PMIC short-press ON |
| (2) No gateway | Start gateway process manually |
| (3) Network path down | Reconfigure remote access, or fall back to LAN direct |
| (4) Docker stopped | `orbctl start` (skip if not building) |
| (5) Config mismatch | Update `sdkconfig.defaults.local` (personal settings, gitignored) |
| (6) Stale build | `rm -rf build releases/*.zip` → rebuild with `release.py stackchan` |

**All 6 OK** before proceeding to flash/monitor/device operations.

## 4. Build / Flash essentials

### Correct build command

```bash
cd firmware
docker run --rm --cpus=4 --ulimit nofile=65536:65536 \
  -v "$PWD":/project -w /project espressif/idf:v5.5.2 \
  python ./scripts/release.py stackchan
```

`release.py stackchan` sets the correct board type (`BOARD_TYPE_STACKCHAN` = M5Stack CoreS3 + Servo). **The `stackchan` argument is mandatory.**

**Do NOT use** `idf.py set-target esp32s3 && idf.py build` — this selects the wrong default board (`BREAD_COMPACT_WIFI`), producing firmware that causes PSRAM mode mismatch boot loops.

`--cpus=4` prevents OOM during LVGL/emoji font compilation on macOS Docker hosts. `--ulimit nofile=65536:65536` prevents `Too many open files` in the same phase.

### Flash (routine iteration)

**Flash `xiaozhi.bin` to `0x20000` to preserve NVS**:

```bash
esptool.py --chip esp32s3 --port /dev/cu.usbmodemXXXX -b 460800 \
  --before default_reset --after hard_reset \
  write_flash 0x20000 build/xiaozhi.bin
```

**Do NOT flash `merged-binary.bin` to `0x0` in routine work** — this overwrites bootloader through partition table and risks NVS data loss.

### Serial monitor (post-flash)

```python
import serial, time
s = serial.Serial('/dev/cu.usbmodemXXXX', 115200, timeout=1)
end = time.time() + 15
while time.time() < end:
    line = s.readline()
    if line: print(line.decode('utf-8', errors='replace').rstrip())
s.close()
```

Do NOT toggle DTR/RTS (this puts the chip into download mode). If USB CDC resets (`Errno 6`), retry with a 1-2 second delay.

## 5. MCP device testing pitfalls

### NVS WS URL override

Devices previously used with other firmware may have stale WS URLs in NVS. The Kconfig fallback (`CONFIG_DEFAULT_WEBSOCKET_URL`) only applies when NVS is empty. To override:

Add to `firmware/sdkconfig.defaults.local` (gitignored):

```
CONFIG_DEFAULT_WEBSOCKET_URL="ws://<your-gateway-ip>:8765/"
CONFIG_FORCE_DEFAULT_WEBSOCKET_URL=y
CONFIG_DEFAULT_WEBSOCKET_TOKEN="<same-token-as-gateway-.env>"
```

**Never commit these settings to `sdkconfig.defaults`** — they contain personal network configuration that would break other users' builds.

### Gateway restart does not trigger ESP32 reconnect

The ESP32 firmware attempts WS connection at boot time. After starting a new gateway, **hard reset the ESP32** to initiate a fresh connection.

### Token mismatch symptoms

If gateway `.env` token and sdkconfig token don't match: the device connects briefly, then disconnects after ~8 seconds (`EspTcp: TCP receive failed: -1`). Align both tokens and restart.

## 6. LAN IP changes

DHCP environments cause IP drift. If `sdkconfig.defaults.local` contains a hardcoded LAN IP:

1. Check current IP: `ipconfig getifaddr en0` (macOS) or equivalent
2. Update `sdkconfig.defaults.local`
3. Rebuild and reflash

For a permanent solution, use DHCP reservation or remote access (see `docs/remote-access.md`).

## 7. Auto sleep behavior (WS disconnected)

When the ESP32 cannot establish a WS connection, it enters progressive sleep:

| Time from boot | Behavior |
|---|---|
| 0-30s | Boot + WiFi + first WS attempt |
| 30-100s | Idle with periodic retry |
| ~100s | `PowerSaveTimer: Enabling power save mode` + backlight dim |
| 100-180s | Progressive deep sleep, eventual USB CDC disconnect |

**Recovery**: USB reconnect or PMIC short-press. Note: PMIC short-press is a full ESP32 POWERON reset (SRAM wiped), not a light wake.

**Strategy**: Establish WS connection before starting verification to suppress the sleep timer.

## 8. ESP32 reconnect after auth failure (Issue #61)

After a gateway-side auth rejection, the ESP32 does not retry reconnection. The workaround is a hard reset:

```bash
esptool.py --before default_reset --after hard_reset chip_id
```

This is tracked as Issue #61.

## 9. Updating the device to latest main

After merging firmware changes to main, the physical device needs a rebuild + reflash:

```bash
cd firmware
rm -rf build releases/*.zip
docker run --rm --cpus=4 --ulimit nofile=65536:65536 \
  -v "$PWD":/project -w /project espressif/idf:v5.5.2 \
  python ./scripts/release.py stackchan
esptool.py --chip esp32s3 --port /dev/cu.usbmodemXXXX -b 460800 \
  --before default_reset --after hard_reset \
  write_flash 0x20000 build/xiaozhi.bin
```

Check `git log HEAD..origin/main --stat` — if only `gateway/` changed, no reflash needed.

## 10. Device validation responsibilities

For PRs touching device-facing MCP tools (`get_head_angles`, `move_head`, `set_avatar`, etc.):

**Maintainer responsibilities** (can be done remotely):
- Docker build, build artifact inspection
- USB serial port discovery, flash, serial monitor
- Boot log analysis, MCP tool behavioral verification
- PR validation checklist completion

**Device owner responsibilities** (physical access required):
- USB cable connect/disconnect
- PMIC power button operation (long-press 6s OFF / short-press ON)
- Physical device repositioning
- Manual head hold for snap-suppress testing

**Destructive action announcement** is the gate condition — always announce before flashing or resetting.

## 11. Common failure patterns

| Symptom | Cause | Fix |
|---|---|---|
| Boot loop `octal_psram: PSRAM chip is not connected` | Built with wrong board (default instead of stackchan) | Rebuild with `release.py stackchan` |
| `OSError: [Errno 6] Device not configured` | USB CDC reset after flash | Retry with 1-2s delay |
| Serial monitor shows 0 lines | Monitor started after boot completed | Hard reset → immediate monitor |
| WS connects to `wss://api.tenclass.net/...` | Stale NVS URL from previous firmware | Add Force mode to `sdkconfig.defaults.local` (§ 5) |
| MCP tools 0 / `connected=false` | Token mismatch between gateway and firmware | Align tokens, restart gateway, hard reset ESP32 |
| `address already in use ('0.0.0.0', 8765)` | Another gateway process running | `lsof -i :8765` to identify, then kill |
| `releases/*.zip already exists` | Stale build artifact | `rm -rf build releases/*.zip` |
| `Cannot allocate memory` during LVGL build | Docker OOM on macOS | Use `--cpus=4 --ulimit nofile=65536:65536` |
| WS connects then disconnects after ~8s | Token mismatch or gateway not restarted after `.env` edit | Align tokens, restart, hard reset |
| No reconnect after `listening → idle` | Auth-failure disconnect bug (Issue #61) | Hard reset ESP32 |
| Stale LAN IP in sdkconfig | DHCP lease renewal | Update `sdkconfig.defaults.local`, rebuild, reflash (§ 6) |
| MCP tools disappear after gateway kill | stdio MCP design — no auto-restart | Restart host or reconnect |
