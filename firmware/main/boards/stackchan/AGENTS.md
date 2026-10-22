# firmware/main/boards/stackchan/ AGENTS.md

Board-specific knowledge for the stackchan configuration (CoreS3 + SCS0009 + Si12T + ILI9342). Covers **servo behavior, touch event classification, WS lifecycle, layer architecture, license boundary, and attribution**.

For general ESP32 build/flash/troubleshooting, see `firmware/AGENTS.md`.

## 1. SCS0009 servo behavior

### Wake-up latency after VM_EN HIGH

After `Servo power ENABLED via PY32 pin 0 (VM EN HIGH confirmed)` (~tick 1275), the SCS0009 needs ~200 ms to wake up. During this window, `ReadPos` and `WritePos` return `-1`. This is **not** a bus hang — it self-clears after 250-350 ms.

| Symptom | Cause | Recovery |
|---|---|---|
| Bus startup latency | VM_EN HIGH → ~200 ms wake-up | Wait 250-350 ms, self-clears |
| Bus hang (Issue #100) | Servo internal protection mode (end-stop contact / stall current) | PMIC long-press 6s OFF/ON required |

Boot-early `Boot pre-init ReadPos: yaw_raw=-1 pitch_raw=-1` (at ~tick 140) is within this latency window. The retry mechanism (5x50 ms, 250 ms total, Issue #138 / PR #139) absorbs this.

### `MotionState::current_deg = 0` initial value pitfall

If `MotionState::current_deg` stays at `0` (ReadPos failed without fallback), subsequent `WriteHeadAngles(0, 45, 4000)` interpolation walks through the end-stop region (pos ~620), causing the "boot snap" sound. Fix (PR #139): ReadPos retry + fallback seed `current_deg = BOOT_INIT_PITCH_DEG (=45)`.

### Physical intervention: hand-hold method

During PMIC ON, holding the head at ~45° by hand while the boot-init climb completes (5-6 seconds) is 100% effective — SCS0009 torque is well below human hand resistance. This is the hardware-level fallback until firmware-side fixes fully eliminate the snap.

## 2. Si12T touch event classification

`TouchPollTick()` polls the Si12T (12-channel capacitive touch IC, I2C 0x68) every 50 ms and feeds `TouchGestureTracker` (`touch_gesture.h`, ESP-IDF-free, unit-tested in `firmware/host_test/test_touch_gesture.cc`). TAP / STROKE are decided from **finger movement across the three head zones**, not from how long the pad was held.

### Chip initialisation (`Si12T::Begin()`, every boot)

The Si12T keeps its registers across an ESP32 reset, so a stuck baseline or the previous firmware's settings survive. `Begin()` therefore follows the datasheet start-up order every time: CTRL=0x0F (soft reset) → 0x07 → SEN1/SEN2=0x33 (M=1.20 %) → CFIG=0x22 → Ch_on1=0xF8 / Ch_on2=0x3F (CH1-3 enabled, others stopped; bit 4/5 of 0x0D stay 1) → Ref_rst1/2=0x00, Cal_on1/2=0x00 → CTRL=0x03. Registers are logged before and after (`Si12T: before:` / `after:`) and read back; a mismatch fails the init (touch stays off, other features unaffected).

### Gesture rules

| Rule | Value |
|---|---|
| `TOUCH_POLL_MS` | 50 |
| Boot grace | first 10 s after init (the chip's fast-calibration window): nothing is reported; a channel still non-zero when the window ends stays unusable until it reads 0 for 1 s |
| Press / release confirm | press = 1 sample, release = 3 samples (150 ms) |
| **STROKE** | during one press the finger left a zone it started on **and** reached a zone it did not start on (each held ≥ 100 ms), or the level-weighted centre moved by one zone, **and** some channel reached level H (3). Reported on release; the face changes the moment the movement is seen |
| **TAP** | no movement, held 100..500 ms, **and** some channel reached level H (3), screen on or off. A press that stayed at L/M is dropped (`touch press ignored (below H)`, still logged): an untouched head shows L/M bursts (sometimes every ~4 s for minutes), while 40 of 43 finger taps recorded in hands-on sessions reached H (a very light tap that peaks at M is the accepted loss) |
| Stationary hold > 500 ms | no event (logged as `touch press ignored`). A resting hand or a stuck channel therefore never fires |
| Stuck channel | a channel non-zero for 15 s gets `Si12T::ResetReference()` (Ref_rst bit), is dropped from the decision, and is used again after 1 s of zeros. The press in progress is not reported. Count in `get_touch_state.recalibrations` |
| Cooldown | 300 ms after the physical release |
| Charging / backlight switch | when the PMIC charging state or the screen on/off state changes, the tracker drops any press, ignores touch for 1 s, then resets the baseline of CH1-3 (`Ref_rst`) and keeps them unusable until each reads 0 for 1 s. Count in `touch: baseline reset after charging/backlight switch` log lines |
| Reaction | TAP: `surprised`, idle after 1.2 s. STROKE: `embarrassed` + servo wobble, idle 1.5 s after the last movement |

The screen is woken (`HandleScreenActivity`) only by a confirmed TAP/STROKE while touch is enabled, so a stuck channel no longer keeps the display on. With touch disabled (`touch/enabled` = false, the default) the poll and the stuck-channel recovery still run — `get_touch_state` stays useful for diagnostics — but nothing is reported, no reaction is shown and the screen-off timer is untouched.

`STROKE` `duration_ms` is the length of the press in which the finger moved (it used to be the hold time).

### Touch guidance for hardware testing

- The touch sensor is the **Si12T on the head top** (not the LCD screen FT6336)
- Capacitive — use finger pad, not fingernail
- STROKE needs the finger to slide from one zone to another (front ↔ back, or one zone over). Resting a hand on the head does nothing by design
- Space consecutive tests at least 1 second apart
- Always include an untouched soak (10 min on USB, 10 min on battery only): TAP/STROKE must stay at 0
- `get_touch_state` also returns `masked` (bit mask of channels dropped as stuck), `recalibrations`, `enabled` and `boot_grace`

### Wobble behavior (post PR #176 fix)

`ServoWobbleStepAdvance()` drives 4 yaw steps: `-A → +A → -A → 0` (A=20°). **Pitch is held at `pitch_motion_.current_deg`** (PR #176 fix). Prior to this fix, `target_pitch = 0` was hardcoded, driving pitch toward the end-stop.

Wobble is **staged** in `StartServoWobble()` (sets flags under `motion_mutex_`) and **executed** in `ServoTaskMain` tick — this avoids races between the touch poll callback (`ESP_TIMER_TASK`) and the servo motion task.

## 3. WebSocket lifecycle pitfalls

### Listening-state touch triggers WS disconnect

In `application.cc`, a touch during `kDeviceStateListening` calls `CloseAudioChannel()` → `websocket_.reset()`, disconnecting the WebSocket. This is problematic for MCP control use cases where the WS should remain open. PR #136 addresses this.

### Boot-time WS auto-connect

`OpenAudioChannel()` is only triggered by user interaction (touch, wake word). Boot does NOT auto-connect. PR #197 adds a boot-time WS auto-connect path.

### LCD display initial state

The avatar is **not shown at boot** — it only appears after a `set_avatar(<face>)` call. Default post-boot/reconnect state is a blank screen. This is tracked as Issue #77 (enhancement: auto-show idle avatar on connect).

## 3.5. Layer architecture (xiaozhi standard vs stackchan overrides)

The stackchan board (`class StackChanBoard : public WifiBoard`, `stackchan.cc:499`) mixes custom implementations with xiaozhi standard passthrough:

### Layer override / passthrough mapping

| Layer | xiaozhi standard | stackchan behavior | Effect |
|---|---|---|---|
| Display backend | `Board::GetDisplay()` | Override → `SpiLcdDisplay` (ILI9342 320x240) | Standard LVGL rendering |
| Status bar | `SetStatus(...)` | Passthrough | Chinese status text, hidden by avatar when active |
| Emotion icon | `SetEmotion(...)` | Passthrough | Hidden by avatar when active |
| **Avatar (custom)** | N/A | `avatar_img_` LVGL image, foreground, 320x240 | Covers all standard UI when active |
| **LED** | `GetLed()` → `NoLed` | No override → NoLed passthrough | **LED does not reflect state** |
| **Mouth animation (custom)** | N/A | `OnTtsStart/Stop` → lip-sync | Active during speaking only |
| **Touch reactive face (custom)** | N/A | `TouchPollTick` → surprised/embarrassed | 1.2-1.5 second face change on TAP / STROKE |
| Backlight | `GetBacklight()` → `nullptr` | Override → `CustomBacklight(pmic_)` | Auto-dim via PowerSaveLevel |
| Camera | `GetCamera()` → `nullptr` | Override → `camera_` | MCP `take_photo` |
| Audio codec | `GetAudioCodec()` → `nullptr` | Override → AW88298 + ES7210 | TTS + mic |

### State observation guide

| State | Deterministic observation | NOT reliable |
|---|---|---|
| idle / listening | MCP `get_device_status` state field, serial log | LCD avatar (no auto-switch), LED (NoLed) |
| speaking | Serial log `tts.start`/`tts.stop`, mouth animation | LED |
| touch reaction | LCD avatar (surprised/embarrassed), serial `touch event:` | LED, MCP state (unchanged) |
| WS connection | Serial log `WS connected/disconnected`, gateway `/health` | Avatar-covered LCD status |

**Central rule**: deterministic state observation uses **MCP `get_device_status` + serial monitor log**. LCD and LED are supplementary only.

### Placeholder avatar ("Chinese face" symptom)

If `avatar_images.local.cc` was not generated before build, the placeholder (1x1 black pixel) is used. The avatar layer renders as 2x2 pixels, exposing the xiaozhi default Chinese UI underneath. Fix:

```bash
cd firmware
python scripts/avatar_convert/convert_avatars.py
rm -rf build releases/*.zip
# rebuild and reflash
```

The `sanity_check.sh` Step 7 detects this before flash.

## 4. License boundary (GPL-3.0 vs MIT)

See `CONTRIBUTING.md` for the full public specification. This section covers the operational check procedure.

### GPL-3.0 files (8, SCServo_lib)

```
firmware/main/boards/stackchan/INST.h
firmware/main/boards/stackchan/SCS.cc
firmware/main/boards/stackchan/SCS.h
firmware/main/boards/stackchan/SCSCL.cc
firmware/main/boards/stackchan/SCSCL.h
firmware/main/boards/stackchan/SCSerial.cc
firmware/main/boards/stackchan/SCSerial.h
firmware/main/boards/stackchan/SCServo.h
```

### Pre-edit check

```bash
# Verify license headers present
grep -nE 'GPL-3.0|GNU General Public License' firmware/main/boards/stackchan/*.{cc,h} | head -20

# Check if target file is GPL or MIT
grep -l 'GPL-3.0\|GNU General' firmware/main/boards/stackchan/<target-file>
```

### Rules

- **Never remove GPL headers** from the 8 files
- **Never include MIT code from GPL files** (reverse-direction include risks viral effect)
- **No new GPL code** — new servo control code uses the MIT `feetech_scs` component
- Phase A (PR #83): canonical build uses MIT path by default, GPL is opt-in fallback
- Phase B (Issue #144, pending): remove SCServo_lib 8 files entirely

### Gateway boundary

`gateway/` (Python, MIT) communicates via WebSocket — a separate process with no GPL contamination risk.

## 5. stack-chan project attribution

See `README.md` `## Trademarks` for the public specification. This section covers the check procedure.

### Canonical credits

| Item | Value |
|---|---|
| **Creator** | Shinya Ishikawa, 2021 |
| **Official org** | `stack-chan/stack-chan` (Apache-2.0) |
| **Trademark** | "StackChan" / "スタックチャン" = registered trademark of Shinya Ishikawa (defensive registration for OSS protection) |
| **Arduino servo lib** | `stack-chan/stackchan-arduino` (MIT, maintainer: Takao Akaki / mongonta0716) |

### Common misspellings to avoid

- ~~`mongonta0716/stack-chan`~~ → use `stack-chan/stack-chan` (org, not personal fork)
- ~~`mongonta555`~~ → `mongonta0716`
- ~~タカヲ~~ → **タカオ** (Takao)

### Pre-commit check

```bash
# Personal fork link should not appear (exclude the -arduino repo)
grep -n 'mongonta0716/stack-chan' <file> | grep -v 'stackchan-arduino'
grep -nE 'タカヲ|mongonta555' <file>

# Trademarks section sanity
grep -A 3 '^## Trademarks' README.md
grep -A 3 '^## 商標' README.ja.md
```

## 6. Board-specific failure patterns

For general failures (build/NVS/WS/sleep), see `firmware/AGENTS.md`. These are servo/touch/WS-lifecycle specific:

| Symptom | Cause | Fix |
|---|---|---|
| Boot `ReadPos: yaw_raw=-1 pitch_raw=-1` | SCS0009 wake-up latency | ReadPos retry (PR #139). See § 1 |
| End-stop walk during boot climb | `current_deg = 0` without ReadPos fallback | ReadPos fallback seed (PR #139). See § 1 |
| "snap" sound + bus hang at boot | PMIC OFF/ON without snap-suppress | Hand-hold at 45° during boot. See § 1 |
| Touch triggers WS disconnect | `CloseAudioChannel()` in listening state | PR #136 (pending). See § 3 |
| Quick tap doesn't trigger wobble | TAP (< 400 ms) vs STROKE (≥ 400 ms) | Hold ≥ 0.4s. See § 2 |
| Wobble drives pitch to end-stop | `target_pitch = 0` hardcode bug | Fixed in PR #176. See § 2 |
| Chinese UI visible instead of avatar | `avatar_images.local.cc` not generated | Run `convert_avatars.py`, rebuild. See § 3.5 |
| `smooth_ui_toolkit` component missing | Submodule not initialized in worktree | `git submodule update --init firmware/components/smooth_ui_toolkit` |
