# Changelog

All notable changes to this repository are documented here.

The format is based on
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Two release series ship from this repository:

- **Gateway** — version tags `vX.Y.Z` track the Python MCP gateway
  published to PyPI as `stackchan-mcp`. Changes appear under a
  `Gateway` subsection of the release entry.
- **Firmware** — version tags `firmware-vX.Y.Z` track the ESP32
  firmware distributed via GitHub Releases (built through
  `firmware/scripts/release.py`). Changes appear under a `Firmware`
  subsection of the release entry. The upstream xiaozhi-esp32 firmware
  version is currently `v2.2.6`; firmware-v* tags advance independently
  of the upstream version.

When a tagged release also makes documentation changes that are not
specific to one side, those go under a `Docs` subsection of the release
entry.

See `CONTRIBUTING.md` for the release promotion process. The firmware
release workflow refuses to publish if the new tag does not have a
matching dated `## [firmware-vX.Y.Z] - YYYY-MM-DD` section in this
file, so promotion of `[Unreleased]` entries is enforced rather than
documented-only.

## [Unreleased]

### Firmware

- Changed: the device no longer auto-enters Listening after a TTS
  utterance ends. The `Application::OnIncomingJson` handler for the
  `tts.stop` event used to fall through to
  `SetDeviceState(kDeviceStateListening)` whenever `listening_mode_`
  was anything other than `kListeningModeManualStop`, mirroring the
  upstream xiaozhi conversational-agent UX. In an MCP-gateway
  deployment that auto-listening path is a footgun: when the TTS
  pipeline stalls (we observed `audio_input` task watchdog timeouts
  firing every 10 s for over two minutes during long playback), the
  deferred `tts.stop` event arrives long after the user expected the
  conversation to be quiescent, and the device then records ~30 s of
  ambient room audio that the gateway happily posts as a user
  utterance — repeatedly per session. With this change `tts.stop`
  unconditionally returns the device to Idle; listening is now started
  only by the user (touch / button / external command) or by the AI
  (gateway-issued `StartListening`). Loop-style `Listening → Speaking
  → Listening` flows belong on the gateway side, where the gateway
  can reason about the conversation's actual state. Avatar mouth
  animation cleanup via `OnTtsStop()` still fires unconditionally.
  Contributed via
  [PR #225](https://github.com/kisaragi-mochi/stackchan-mcp/pull/225).

- Added: when the primary NVS `websocket.url` is empty, the firmware can discover a local stackchan-mcp gateway via mDNS before falling back to `CONFIG_DEFAULT_WEBSOCKET_URL`. The feature is controlled by `CONFIG_STACKCHAN_MDNS_DISCOVERY` and keeps the existing WebSocket candidate fallback loop.

- Added: `self.port_b.ws2812.{init, set_pixel, set_strip, refresh, clear}`
  MCP tools — five generic tools to drive any WS2812-compatible LED
  strip attached to the official kit's Port B (CoreS3 HY2.0-4P digital
  OUTPUT, GPIO 9). Same hardware-boundary-as-contract pattern as the
  Port A I2C generic tools from
  [PR #196](https://github.com/kisaragi-mochi/stackchan-mcp/pull/196):
  the base firmware exposes a generic capability of the official
  expansion port at the MCP layer; accessory-specific semantics live
  outside the base repository. Driver: `espressif/led_strip` ~3.0.2
  (already a dependency in `firmware/main/idf_component.yml`; the
  stackchan board is the first stackchan-side consumer), RMT backend,
  single strip per `init`, `led_count` set at `init()` time (1..256
  parameter-clamped). Hardware path is fully independent of the
  on-board PY32-driven 12-LED base strip (I2C → PY32 → PY32-internal
  WS2812 engine, separate from RMT); existing `self.led.*` behaviour
  is byte-for-byte unchanged. Contributed via
  [PR #223](https://github.com/kisaragi-mochi/stackchan-mcp/pull/223).

- Fixed: the Si12T head-touch driver's TAP / STROKE log line printed
  `duration=lums raw=0xNNNN` instead of human-readable values, because
  ESP-IDF's nano-printf cannot parse the `%llums` specifier and the
  failed parse stops `va_arg` from advancing past `duration_ms` — so
  the following `%02X` consumed the long-long's bytes rather than the
  Si12T Output1 register. `zones=000 raw=0x00` was also misleading
  because the snapshot fields were overwritten every poll tick and
  read the post-release zero state by the time the falling-edge
  handler logged. The log line now uses `%u ms`, captures the
  rising-edge sensor state separately into `press_start_*`, and reports
  it as
  `start_zones=NNN start_raw=0xNN ch=CCCC release_raw=0xNN duration=NN ms`,
  with `ch=CCCC` decoding Output1's four 2-bit channel levels as
  `0/L/M/H` (CH4 should always be `0`; a non-`0` CH4 character flags
  wiring noise / EMI). `HandleTap` also receives the duration so the
  400-600 ms grey-zone TAPs print their timing. The rising-edge
  capture also fires on the cooldown-suppressed branch so a touch
  whose press started during the post-reaction cooldown but released
  after cooldown expired logs its own start state instead of the
  previous touch's. Purely a logging change — touch detection logic
  and timing constants are unchanged. Contributed via
  [PR #206](https://github.com/kisaragi-mochi/stackchan-mcp/pull/206).

- Added: dynamic avatar-set transfer pipeline. A new `AvatarSet`
  scaffold (layered face / eyes / mouth, ~537 KB total in RGB565)
  can be staged on the gateway and fetched into PSRAM at runtime via
  a new `avatar_set_fetch` WebSocket protocol, replacing the old
  build-time `avatar_images.cc` table. The fetch path uses a small
  `AvatarSetFetcher` HTTP client that streams the raw payload
  straight into a PSRAM staging buffer, validates SHA256, then hands
  the buffer over to `AvatarSet::AdoptOwnedBuffer()` (an
  ownership-transfer load, no memcpy). This caps the load-time
  PSRAM peak at `old + new_size` instead of the pre-fix `old +
  staging + new_size`, which was overshooting the 8 MB PSRAM budget
  in matrix-mode (~3.3 MB) loads and producing `Load: PSRAM
  allocation failed (size=3456000)`. Expression-change commands
  arriving during a fetch are deferred until completion so the
  display does not flicker between the old set and the partially
  loaded new set. A small `%zu` → `%u` adjustment in two ESP_LOG
  call-sites keeps nano-printf from crashing on those format
  specifiers. Contributed via
  [PR #210](https://github.com/kisaragi-mochi/stackchan-mcp/pull/210).

- Fixed: `Protocol::SendText` was protected, so board-side code that
  wants to push a board-initiated WS message (e.g. an `avatar_set_loaded`
  reply from the AvatarSet fetcher) had no clean entry point.
  Exposed as public; no behavioral change for existing callers.
  Contributed via
  [PR #210](https://github.com/kisaragi-mochi/stackchan-mcp/pull/210).

- Fixed: a server-side close arriving between the WebSocket server
  hello and the main task resuming no longer cancels the reconnect
  timer that the per-socket `OnDisconnected` lambda just armed.
  `OpenAudioChannelInternal()` now uses a per-candidate
  acquire/release atomic flag captured by `OnDisconnected` (stored
  with release before `ScheduleReconnect()`, loaded with acquire
  immediately after `xEventGroupWaitBits()` returns the server-hello
  event) and bails out with `return false` before the success-path
  `StopReconnectTimer()`. The earlier `websocket_->IsConnected()`
  guard was insufficient because the underlying `connected_` is a
  plain bool with no acquire/release ordering against the close-side
  callback path. The reconnect-timer caller observes the false return
  and waits for the already-armed retry. Happy path (no concurrent
  close) is unchanged. Closes
  [#189](https://github.com/kisaragi-mochi/stackchan-mcp/issues/189).

- Fixed: the `OGG_POPUP` listening cue was only triggered on wake-word
  activation paths (`HandleWakeWordDetectedEvent` /
  `ContinueWakeWordInvoke`), so callers of the public
  `Application::StartListening()` API — board-level touch buttons,
  server-driven listen, etc. — silently lost the audible "listening
  started" feedback. `HandleStartListeningEvent` now arms
  `play_popup_on_listening_` itself, after the early-return branches
  for `kDeviceStateActivating` / `kDeviceStateWifiConfiguring` /
  null-protocol but before the Idle / Speaking-abort listening
  dispatches, so the flag is only latched when the function is
  actually going to transition the device toward listening. The
  public `Application::StartListening()` stays a thin event setter
  (`xEventGroupSetBits` + return) so the write happens entirely on
  the main task, matching the wake-word path that already flips the
  flag from the main task before calling `ContinueWakeWordInvoke`.
  No behaviour change for the wake-word path (the flag was already
  true there before this function ran). Contributed via
  [PR #207](https://github.com/kisaragi-mochi/stackchan-mcp/pull/207).

- Added (stackchan): touch-driven listen UX overhaul. The FT6336
  short-tap behavior now models stack-chan as a single-shot
  push-to-talk device (touch → listen → touch → submit) rather than
  xiaozhi's default continuous-conversation model. Concretely: (a) a
  short tap while in `kDeviceStateListening` now calls
  `StopListening()` instead of `CloseAudioChannel()`, so the gateway
  receives `listen.stop` (= record-then-flush) instead of a transport
  teardown that aborts the in-flight capture; (b) a short tap from
  idle / speaking now calls `StartListening()` (which forces
  `ManualStop`) instead of `ToggleChatState()` (which uses
  `AutoStop`), so the device does not re-arm listening immediately
  after `tts.stop` and the next tap is reliably interpreted as a new
  `listen.start`; (c) 300 ms press-after-release debounce on
  FT6336 chatter; (d) 30-second auto-`StopListening` for users who
  start a listen and then walk away; (e) RGB LED tap feedback (green
  on activation, off on submit) via a small `SetAllRgbLeds` helper
  that shares the I2C path used by the `set_all_leds` MCP tool;
  (f) `%lld` → `%d (int)cast` for the existing duration log,
  matching the nano-printf-safe pattern already used in this file's
  motion driver. Stack-chan-only — other board UX is unchanged.
  Contributed via
  [PR #208](https://github.com/kisaragi-mochi/stackchan-mcp/pull/208).

- Fixed: WebSocket candidate fallback is now fail-fast when a server
  hello is malformed (missing/non-string `transport`, missing/empty
  `session_id`, or unsupported `transport`). A new
  `WEBSOCKET_PROTOCOL_SERVER_HELLO_FAILED` event bit is set by
  `ParseServerHello()` on each rejection path, and
  `OpenAudioChannelInternal()` now distinguishes the three outcomes
  (success, rejection, full timeout). Rejected candidates fall back
  to the next URL within ~100 ms instead of waiting out the full
  10 s server-hello timeout. Happy path and no-hello timeout path
  are byte-for-byte unchanged. Contributed via
  [PR #205](https://github.com/kisaragi-mochi/stackchan-mcp/pull/205).

- Added: generic Grove Port A I2C bus and four `self.i2c.*` MCP tools
  (`scan` / `read` / `write` / `write_read`) so attached M5Stack Unit
  modules (ENV III, ToF, gas sensor, PaHub, etc.) can be driven from
  the gateway without recompiling firmware per Unit. Port A runs on
  I2C controller 0, physically independent from the controller-1
  internal bus (PMIC, AW9523, touch, audio codec, IMU), so the generic
  tools cannot accidentally reach safety-critical ICs. `addr` is
  constrained to 0x08..0x77 (I2C reserved ranges excluded), and
  `bytes` / `write_bytes` payloads are capped at 256 items (matching
  the `n_bytes` read cap). Contributed via
  [PR #196](https://github.com/kisaragi-mochi/stackchan-mcp/pull/196).

- Added: `kPropertyTypeArray` for MCP tool parameters, supporting
  integer arrays (with optional per-element range) and string arrays.
  An optional `set_max_items` setter caps the array length in the
  emitted JSON Schema (`maxItems`) and rejects oversized arrays in
  `set_value()` validation before they reach the tool callback. (The
  parse-time `std::vector::reserve(array_size)` in `DoToolCall` still
  runs before the cap is checked, so the cap acts as a
  payload-acceptance guard rather than a pre-allocation guard;
  tightening to a pre-allocation guard is tracked separately.) The
  generic default-value constructor explicitly rejects
  `kPropertyTypeArray` so future array tools must use the
  element-type-aware constructor. Contributed via
  [PR #195](https://github.com/kisaragi-mochi/stackchan-mcp/pull/195).

- Fixed: WiFi association now retries with a brief delay and cancels
  the in-flight `esp_wifi_connect()` on attempt timeout (via
  `esp_wifi_disconnect()` on the `bits == 0` path), so access points
  that respond slowly or drop the first 802.11 Association Comeback
  frame no longer cause the device to fail WiFi at boot. The retry
  log message also distinguishes timeout vs. driver-reported failure
  to ease real slow-AP debugging. Contributed via
  [PR #186](https://github.com/kisaragi-mochi/stackchan-mcp/pull/186).

### Gateway

- Fixed: mDNS now advertises the host's IPv4 addresses ordered by LAN
  reachability instead of in interface-enumeration order. On a host with
  multiple interfaces (a CGNAT `100.64.0.0/10` overlay address, several LAN
  segments), the firmware tries candidates in advertised order and could spend
  several seconds timing out on an unreachable candidate before reaching a
  LAN-reachable one. Addresses are now stably sorted so RFC1918 private ranges
  (`192.168.0.0/16`, `10.0.0.0/8`, `172.16.0.0/12`) are tried first and
  everything else follows; no reachable address is dropped. Subnet network and
  broadcast addresses (e.g. a `.0` network address or `.255`-style broadcast),
  which are never valid host endpoints, are now excluded when the interface
  prefix is known — guarded so `/31` and `/32` host addresses are never
  dropped. Contributed via
  [PR #232](https://github.com/kisaragi-mochi/stackchan-mcp/pull/232).

- Added: the gateway now advertises `_stackchan-mcp._tcp.local.` over mDNS/DNS-SD by default so fresh firmware can discover the WebSocket endpoint on the local network. A new `--no-mdns` flag disables advertising.

- Added: `send_pcm_audio(gateway, pcm, source_rate=...)` helper
  extracted from `synthesize_and_send`'s encode-and-push back-half.
  External producers — HTTP PCM bridges, sound-effect players,
  alternative voice stacks that already produce PCM — can now push
  pre-synthesised audio to the device without registering a
  `TTSEngine`. `synthesize_and_send` is unchanged from the caller's
  perspective; it now delegates to `send_pcm_audio` after running
  the engine. Resampling to the device's 16 kHz mono is done via
  the existing `resample_pcm16_linear` helper, so producers at
  non-device rates work without manual resampling. Contributed via
  [PR #TBD-A1](https://github.com/kisaragi-mochi/stackchan-mcp/pull/TBD-A1).

- Added: `load_avatar_set` MCP tool + supporting HTTP staging /
  WebSocket fetch protocol for the firmware's dynamic AvatarSet
  pipeline (PR-E1 firmware side). The gateway stages a raw RGB565
  payload on its capture HTTP server (one-time fetch, Bearer-token
  auth, GC'd after 120 s), notifies the device over WebSocket, and
  awaits the device's `avatar_set_loaded` reply. The tool takes a
  filesystem `archive_path` rather than inline bytes so the MCP JSON
  transport stays free of multi-MB base64 payloads. Contributed via
  [PR #210](https://github.com/kisaragi-mochi/stackchan-mcp/pull/210).

- Fixed: `pip install stackchan-mcp[tts]` did not work out-of-the-box
  on Windows because `opuslib` calls `find_library("opus")`, which on
  Windows requires a discoverable `opus.dll` — but pip-installable
  upstream opus binaries do not exist for Windows. The publish
  workflow now produces a platform-specific `*-win_amd64.whl` that
  bundles `opus.dll` built from upstream Opus source via a pinned
  vcpkg release (`VCPKG_PIN=2026.04.27` → Opus 1.5.2 port-version 1)
  on a Windows runner, with the produced DLL's SHA256 verified against
  an `EXPECTED_OPUS_DLL_SHA256` env in the workflow before the wheel
  is built (the build fails on mismatch or unset expected value, so a
  silent vcpkg-side or runner-image drift cannot ship a different
  native binary than the one reviewed for the release). The sdist
  and `py3-none-any` wheel published from the Ubuntu runner ship
  without the binary so non-Windows installs and non-x64 Windows
  installs stay clean. The Windows job also installs the `tts` extra
  (`uv sync --frozen --extra tts`) before the post-build smoke test
  so the test exercises the same `opuslib` import path users hit,
  closing a release-only `ModuleNotFoundError` gap PR CI did not
  cover. The package init prepends `stackchan_mcp/_libs/` to `PATH`
  and registers it via `os.add_dll_directory()` (handle retained at
  module scope so the registration survives garbage collection),
  guarded on `platform.machine() == "AMD64"` so a sdist install on
  a non-x64 Windows host falls back to the same clean "no opus"
  failure mode it had before this fix instead of trying to load an
  architecture-mismatched DLL. The native binary itself is no longer
  tracked in git — see `gateway/.gitignore` and
  `stackchan_mcp/_libs/SOURCES.md`. The BSD-3-Clause + Xiph notice
  for the bundled `opus.dll` ships alongside the gateway's MIT
  `LICENSE` as `LICENSE-THIRD-PARTY`, included in every distribution
  form so license scanners pointed at any wheel variant can see it.
  Contributed via
  [PR #217](https://github.com/kisaragi-mochi/stackchan-mcp/pull/217).

- Added: MCP tool surface for the firmware-side Grove Port A generic
  I2C bus introduced in
  [PR #196](https://github.com/kisaragi-mochi/stackchan-mcp/pull/196) —
  `i2c_scan` (discover attached Units), `i2c_read` (raw read at a 7-bit
  address), `i2c_write` (raw write), and `i2c_write_read`
  (Repeated-Start write-then-read for the register-pointer idiom).
  Exposes the firmware tools so LLM clients can drive attached
  M5Stack Unit modules from the gateway side.

- Fixed: MCP tool schemas for `i2c_read` / `i2c_write` / `i2c_write_read`
  now constrain the `addr` parameter to the I2C 7-bit address range
  `0x08..0x77`, matching the `i2c_scan` probe range and the firmware-side
  `Property` range introduced in
  [PR #196](https://github.com/kisaragi-mochi/stackchan-mcp/pull/196).
  The previous JSON Schema advertised `0..127`, which let LLM callers
  attempt reserved-range addresses (0x00 General Call, 0x78–0x7F
  reserved); the firmware tool property still rejected these at the
  request-handling layer, so this closes a tool-surface inconsistency
  rather than a functional gap. Tool descriptions now state the
  constraint explicitly.

### Docs

- Added: tracked `AGENTS.md` files at four levels (root, `gateway/`,
  `firmware/`, `firmware/main/boards/stackchan/`) with review guidelines
  for automated code review and public developer guides covering build,
  flash, troubleshooting, servo/touch behavior, layer architecture,
  license boundary, and attribution. Previously all `AGENTS.md` files
  were gitignored; personal local configuration now uses
  `AGENTS.local.md` (gitignored). Added migration guide and review
  priorities section to `CONTRIBUTING.md`.

## [firmware-v1.8.0] - 2026-05-20

### Firmware

- Fixed: device booted to `idle` without connecting to the gateway,
  leaving MCP tools (`set_avatar`, `set_leds`, `move_head`, etc.)
  unreachable until a touch or wake-word trigger first opened an
  audio session. `WebsocketProtocol::Start()` now connects to the
  configured gateway at boot via `OpenAudioChannelInternal(report_error=false,
  arm_audio_channel=false)`, decoupling the physical WebSocket
  transport from the logical audio-session state introduced in
  [#192](https://github.com/kisaragi-mochi/stackchan-mcp/pull/192).
  Two coupled fixes ride along: (a) the `OpenAudioChannelInternal`
  failure-exit now clears `intentional_close_` and arms
  `ScheduleReconnect()` so a gateway-down boot (or any subsequent
  failed retry) continues to retry automatically instead of silently
  latching into a no-reconnect state — restoring the apparent intent
  of the constructor's reconnect-timer lambda, whose own redundant
  `ScheduleReconnect()` call is removed to prevent double-advancing
  `reconnect_interval_ms_` per failed retry; (b)
  `Application::CanEnterSleepMode()` now also blocks the legacy
  60-second `PowerSaveTimer` deep-sleep entry while a new
  `Protocol::IsTransportConnected()` virtual returns true, closing
  the regression in which a boot-time WS connect with
  `arm_audio_channel=false` would still trip the timer because
  `audio_channel_open_` stays false. The transport-state predicate
  is backed by a `std::atomic<bool> transport_connected_` flag
  updated at server-hello success / disconnect / prologue /
  destructor, avoiding the use-after-free race that would result
  from reading `websocket_` directly on `ESP_TIMER_TASK`. External
  reproduction confirmation by @tjkang. Closes
  [#169](https://github.com/kisaragi-mochi/stackchan-mcp/issues/169).

- Fixed: WebSocket was torn down on every user-initiated
  `CloseAudioChannel()` (touch in listening mode, audio session abort),
  making MCP control surfaces (LEDs, avatar, head movement, etc.)
  unreachable until the next wake / touch reopened the channel. The
  teardown is now skipped — the device transitions back to idle while
  the WebSocket stays connected for continued MCP control. Contributed
  via
  [PR #136](https://github.com/kisaragi-mochi/stackchan-mcp/pull/136).

- Fixed: gateway-driven `tts.start` / `listen.start` MCP messages were
  silently dropped after a user-initiated `CloseAudioChannel()`,
  because the audio-channel-state drop guard introduced alongside
  [#136](https://github.com/kisaragi-mochi/stackchan-mcp/pull/136)
  did not distinguish current-session messages from stale ones.
  Replaced the binary `audio_channel_open_` predicate with a
  `session_id` mismatch check: current-session messages flow through
  to drive the firmware state machine while stale messages from
  superseded WebSocket sessions remain blocked. Binary-frame gating
  now runs up-front via an early `kDeviceStateSpeaking` check,
  avoiding `BinaryProtocol2/3` header parse and `AudioStreamPacket`
  payload allocation on the closed-channel path. The public WebSocket
  protocol contract (`firmware/docs/websocket.md`,
  `firmware/docs/websocket_zh.md`) now requires a non-empty string
  `session_id` in the server hello. Carved-out follow-ups:
  [#190](https://github.com/kisaragi-mochi/stackchan-mcp/issues/190)
  (audio-operation-level identifier for same-session delayed
  messages),
  [#191](https://github.com/kisaragi-mochi/stackchan-mcp/issues/191)
  (fail-fast on invalid server hello). Closes
  [#187](https://github.com/kisaragi-mochi/stackchan-mcp/issues/187).

### Docs

- Corrected the stack-chan project attribution in `README.md` and
  `README.ja.md`. The hero blurb, the self-built-stack-chan note,
  and the "Related projects" entry previously credited the project
  to "Takawo-san" (mongonta0716 / Takao Akaki) and linked to a
  personal fork; the project originator is **Shinya Ishikawa**
  (ししかわ / 石川真也), with public release in 2021, and the
  canonical upstream is `stack-chan/stack-chan`. The "Related
  projects" section now also separately credits Takao Akaki
  (mongonta0716) via the `stack-chan/stackchan-arduino` entry,
  which is the implementation lineage actually referenced by this
  firmware. A new `Trademarks` / `商標` section is appended to
  both READMEs acknowledging that "StackChan" / "スタックチャン"
  is a registered trademark of Shinya Ishikawa. Closes
  [#184](https://github.com/kisaragi-mochi/stackchan-mcp/issues/184).


## [0.8.0] - 2026-05-19

### Gateway

- Added the `set_auto_torque_release(enabled, timeout_ms)` MCP tool
  exposure on the gateway, the runtime configuration surface for the
  firmware-side Phase 4 auto-torque-release feature
  ([#152](https://github.com/kisaragi-mochi/stackchan-mcp/issues/152)
  Phase 4). `timeout_ms` is clamped by the firmware to `500..600000`
  ms; the gateway forwards the request and returns the firmware's
  response including the `clamped` flag and the
  `torque_released_at_call` state. Refs
  [#168](https://github.com/kisaragi-mochi/stackchan-mcp/issues/168).

- Added the `set_servo_torque(yaw_enabled, pitch_enabled)` MCP tool
  exposure on the gateway. The tool is a per-axis SCS0009 torque
  toggle primitive originally introduced as a diagnostic probe for the
  Phase 4 design work but also useful as a standalone power-management
  primitive. The gateway forwards the request and returns the
  firmware's response including the `short_circuited` flag indicating
  whether the bus call was actually issued. Closes
  [#163](https://github.com/kisaragi-mochi/stackchan-mcp/issues/163).

- Added hardware-lane aware dispatch for ESP32 tool calls. Independent
  hardware lanes (servo, LED, avatar/display, screen, audio, camera,
  touch, status) now pipeline concurrently on the gateway side, while
  ordering within the same lane is preserved. The existing
  `ESP32Manager.call_tool()` API remains compatible. `tools/call`
  send-failure handling is also hardened: WebSocket send failures now
  mark the ESP32 connection disconnected and no longer leave
  unobserved pending future exceptions. Refs
  [#73](https://github.com/kisaragi-mochi/stackchan-mcp/issues/73)
  (firmware-side `tools/call` execution remains serialized by
  `Application::Schedule()`, so Issue #73 stays open as the
  firmware-side follow-up).


## [firmware-v1.7.0] - 2026-05-19

### Firmware

- Fixed: STROKE-triggered touch wobble previously commanded
  `target_pitch = 0` per step, forcing the SCS0009 pitch axis toward
  the lower mechanical end-stop (raw `pos ≈ 620`) on every touch. The
  wobble now preserves the pre-wobble pitch by reading
  `pitch_motion_.current_deg` (already protected by the active
  `motion_mutex_` hold at this point in `ServoWobbleStepAdvance()`);
  yaw continues to oscillate `±SERVO_WOBBLE_AMPLITUDE_DEG`. This
  eliminates the single-STROKE `#165` onset path that
  [#146](https://github.com/kisaragi-mochi/stackchan-mcp/pull/146)
  introduced by hardcoding the pitch target, which subsequently became
  reachable on every touch once
  [PR #173](https://github.com/kisaragi-mochi/stackchan-mcp/pull/173)
  made `auto_idle → reengagement → wobble` a routine cycle. On-device
  verification: single STROKE shows `target_deg=44` (pre-wobble pitch)
  across all four wobble steps; eight strokes in 60 s and a 60-min
  representative motion load each report zero `WritePos retries
  exhausted` events. Closes
  [#175](https://github.com/kisaragi-mochi/stackchan-mcp/issues/175).

- Added `set_auto_torque_release(enabled, timeout_ms)` and automatic
  SCS0009 torque release after motion idle timeout for #152 Phase 4.

- Fixed: mitigated #165 cumulative WritePos protection-mode exposure by
  reducing session-wide WritePos accumulation during idle periods.

- Added a `self.robot.set_servo_torque(yaw_enabled, pitch_enabled)`
  MCP tool that toggles SCS0009 torque on each axis independently via
  `ScsBus::EnableTorque(id, enable)`. Originally introduced as a
  diagnostic probe for the #152 Phase 4 auto-torque-release design,
  the tool also stands on its own as a power-management primitive.
  On disable, the firmware cancels any in-flight MotionDriver
  interpolation, marks the axis position unknown, bumps the per-axis
  freshness token (via `InvalidateAxisToken`), and cancels any
  in-flight wobble sequence before issuing the `EnableTorque` bus
  frame; this narrows the window in which a stale `Tick` snapshot
  could emit one last `WritePos`. On enable, the bus frame is the
  only side effect; re-anchoring `current_deg` is left to the caller
  (`get_head_angles` + a fresh `set_head_angles` is the documented
  pattern). Closes
  [#163](https://github.com/kisaragi-mochi/stackchan-mcp/issues/163).

- Fixed the `ServoDelegatedMotionDriver` Phase 0' / `set_servo_torque`
  cancellation boundary by adding an `InvalidateAxisToken(axis_id)`
  override that both bumps the per-axis `request_token` and clears the
  per-`AxisServo` private dispatch / retry state
  (`pending_dispatch_`, `dispatch_failures_`,
  `readmove_failures_`) atomically under the caller-held
  `motion_mutex_`. Without the private-state reset, a pre-reset
  `Stage()` could leave `pending_dispatch_=true`, causing the next
  `Update()` tick to issue a `WritePos` for the stale staged target
  even though the visible `AxisMotion` fields had been reset by
  Phase 0' or by the `set_servo_torque` disable path. The host
  interpolation path is unaffected (no equivalent private-state
  surface), so this PR completes the cancellation boundary on the
  delegated path. Closes
  [#160](https://github.com/kisaragi-mochi/stackchan-mcp/issues/160).

- Fixed a same-millisecond `StartMove` race in
  `HostInterpolationMotionDriver` by replacing the `move_start_ms`
  equality check used for `Tick()` write-back freshness with a
  per-driver monotonic `next_request_token_` counter. The
  `move_start_ms` field is sourced from
  `esp_timer_get_time() / 1000` (1 ms resolution), so two `StartMove`
  calls landing inside the same millisecond shared the guard value
  and a stale `Tick` snapshot could overwrite the newer move's state.
  The `next_request_token_` counter is incremented in `StartMove` and
  written into `AxisMotion::request_token` (a struct field that has
  existed since PR #154 / Phase 2 and was previously only consumed by
  `ServoDelegatedMotionDriver`). To preserve the freshness invariant
  across Phase 0' direct `AxisMotion` mutations, this PR also adds
  a new `MotionDriver::InvalidateAxisToken(axis_id)` hook with a
  base-class no-op default; `HostInterpolationMotionDriver` overrides
  it to bump the per-driver counter, and `InitializeServo()` Phase 0'
  calls the hook inside its existing `motion_mutex_` critical section
  after every direct `AxisMotion` mutation. `move_start_ms` is
  retained for its arithmetic role in `AdvanceAxisLinear`
  (`elapsed = now_ms - move_start_ms`); only its identity / freshness
  role is replaced. Closes
  [#158](https://github.com/kisaragi-mochi/stackchan-mcp/issues/158).

- #152 Phase 3 — replaced normal-runtime `HostInterpolationMotionDriver`
  linear interpolation with `smooth_ui_toolkit` spring physics
  (`AnimateValue` per axis, m5stack/StackChan-equivalent default spring,
  `duration_ms`-driven stiffness/damping mapping, and real-elapsed-time
  spring ticks). The default `CONFIG_STACKCHAN_SERVO_FEETECH=y` path now
  uses natural-spring host-side interpolation for MCP `move_head` and touch
  wobble; boot-time `InitializeServo()` slow climb remains on
  duration-bounded linear interpolation to avoid wake-up snap motion. The
  `CONFIG_STACKCHAN_SERVO_DELEGATED_MOTION=y` opt-in path is unchanged
  from Phase 2 / PR #154.

## [firmware-v1.6.0] - 2026-05-17

### Firmware

- Added a post-init `ReadPos` re-sync step ("Phase 0'") to
  `InitializeServo()` that re-reads the SCS0009 actual position after
  the boot-init `WriteHeadAngles` interpolation completes and
  overwrites `pitch_motion_.current_deg` / `yaw_motion_.current_deg`
  with the actual physical position. This eliminates the firmware-side
  / SCS0009-actual mismatch that the #138 safe-fallback intentionally
  leaves behind on the PMIC long-press OFF / ON boot path, where
  Phase 0's `WriteHeadAngles(0, 45, ...)` is a no-op of effect because
  `current_deg` was seeded equal to the target. Without Phase 0',
  `move_head(0, 45)` immediately after such a boot returned
  `pitch_motion_started: 0` (firmware sees `current_deg == target`, no
  interpolation starts), and `get_head_angles` returned the actual
  position (e.g. 38°) — a contradictory observation hard to attribute
  correctly without firmware-internals knowledge. Phase 0 is also now
  distance-aware: its duration is computed from the actual
  `current_deg → BOOT_INIT_*_DEG` delta at a new
  `BOOT_INIT_TARGET_DEG_PER_SEC = 15 °/s` cap, floored at
  `BOOT_INIT_MOVE_MS = 3000 ms` to keep the SCS0009 wake-up latency
  window covered. New `BOOT_INIT_TARGET_DEG_PER_SEC` constant is
  additive; existing `BOOT_INIT_YAW_DEG=0` / `BOOT_INIT_PITCH_DEG=45`
  semantics preserved. Closes
  [#141](https://github.com/kisaragi-mochi/stackchan-mcp/issues/141).

- Refactored `ServoDelegatedMotionDriver` (opt-in via
  `CONFIG_STACKCHAN_SERVO_DELEGATED_MOTION=y`) so that bus dispatch
  and `ReadMove` polling happen per-axis under a short-hold
  `scs_bus_mutex_`, removing the bundled two-axis critical section.
  This eliminates the necessary side of the residual hang trigger
  documented in PR #146 (two-axis simultaneous dispatch combined with
  pitch end-stop proximity or cumulative load). Internal `AxisServo`
  private nested class introduced; public `MotionDriver` interface
  unchanged. `HostInterpolationMotionDriver` (default Kconfig path)
  is byte-equivalent. (#152 Phase 2)

- Added [`smooth_ui_toolkit`](https://github.com/Forairaaaaa/smooth_ui_toolkit)
  v2.12.0 (MIT, Copyright (c) 2023 Forairaaaaa) as a git-submodule
  ESP-IDF component dependency under
  `firmware/components/smooth_ui_toolkit/`, wired into the main
  component via `PRIV_REQUIRES`. No source-level consumer yet; this
  stages the dependency for the upcoming motion-subsystem migration
  tracked in
  [#152](https://github.com/kisaragi-mochi/stackchan-mcp/issues/152)
  (Phase 1). Contributors building from source must run
  `git submodule update --init --recursive` after pulling.

- Added a `MotionDriver` abstraction for StackChan servo motion and an
  opt-in `CONFIG_STACKCHAN_SERVO_DELEGATED_MOTION` path that delegates
  move timing to the SCS0009 via single-shot `WritePos(..., time, 0)`
  commands plus `ReadMove()` polling, while keeping the existing
  host-interpolation path as the default fallback (default `n`). The
  MCP tool surface is unchanged. Real-device verification on
  M5Stack CoreS3 + SCS0009 ×2 shows the delegated path substantially
  mitigates the bus-hang surface historically tracked under
  [#100](https://github.com/kisaragi-mochi/stackchan-mcp/issues/100):
  single-axis large-angle reversals, two-axis moves with mid-range
  pitch from a clean state, and pitch-only large-angle reversals
  (including end-stop proximity) all complete cleanly. A residual hang
  trigger requiring two-axis simultaneous dispatch combined with either
  pitch end-stop proximity or cumulative session load remains;
  mitigations are split to
  [#147](https://github.com/kisaragi-mochi/stackchan-mcp/issues/147) /
  [#148](https://github.com/kisaragi-mochi/stackchan-mcp/issues/148) /
  [#149](https://github.com/kisaragi-mochi/stackchan-mcp/issues/149).
  A related boot-init `current_deg` mismatch corner case after PMIC
  OFF/ON is tracked under
  [#150](https://github.com/kisaragi-mochi/stackchan-mcp/issues/150).
  Default flip is deferred to a subsequent release after these
  mitigations land. The `position_unknown` sentinel detects and reports
  any residual trip on the firmware side; recovery still requires
  PMIC OFF/ON. Refs
  [#143](https://github.com/kisaragi-mochi/stackchan-mcp/issues/143).

## [firmware-v1.5.0] - 2026-05-16

### Firmware

- Added a boot-time snap-suppress hold to `InitializeServo()` to
  mitigate the [#121](https://github.com/kisaragi-mochi/stackchan-mcp/issues/121)
  Problem 1 "downward drop on power-on" symptom. Immediately after the
  existing pre-init `ReadPos` diagnostic, the firmware now issues a
  `WritePos(id, current_pos, time=0, speed=0)` per servo, which the
  SCS0009 treats as a new target equal to its current position and uses
  to truncate any in-progress "snap-to-last-target" motion. Background:
  the SCS0009 retains its commanded set-point across power cycles
  (Hypothesis 1 in #121, confirmed by the firmware-v1.4.1 clean-install
  reproduction in which `Boot pre-init ReadPos` still matched the
  pre-power-off pose exactly after a full NVS reset on the ESP32 side,
  demonstrating the set-point lives in the servo itself). When `VM_EN`
  asserts at hardware power-on the servo restores torque and snaps
  toward that retained target before any firmware-side speed limiting
  can apply, audible as a mechanical end-stop impact when the previous
  session ended near `pitch=0°`. Efficacy is observable in the serial
  log via new `Boot snap-suppress yaw/pitch hold(pos=...): r=...`
  lines; if `ReadPos` already captured an end-stop position the hold is
  a no-op for that boot and a deeper fix (e.g. firmware-controlled
  `VM_EN` sequencing through the PY32 IO-expander) would be required,
  tracked separately. The pitch hold is additionally gated on the
  `ReadPos` raw value falling inside the same `SAFE_PITCH_MIN..
  SAFE_PITCH_MAX` range as every other pitch servo-write boundary in
  the firmware: out-of-range reads (e.g. the head was hand-pushed past
  an end-stop pre-boot, or the previous session's set-point fell
  outside the safe range) skip the hold and let the subsequent
  interpolating boot-init climb to `(yaw=0°, pitch=45°)` drive the
  head back into the safe range through the existing speed-limited
  path, instead of pinning the servo against an out-of-range raw
  position. Refs
  [#121](https://github.com/kisaragi-mochi/stackchan-mcp/issues/121)
  Problem 1.

- Extended the boot-time snap-suppress hold added for #121 Problem 1
  (PR #137) so that it actually fires on the PMIC long-press OFF / ON
  path, by retrying the pre-hold `ReadPos` long enough to absorb the
  SCS0009 `~200 ms` startup latency after `VM_EN` HIGH. Each `ReadPos`
  in `InitializeServo()` is now attempted up to 5 times at 50 ms
  intervals (250 ms total budget per axis), well above the observed
  wake-up latency window. The `Boot pre-init ReadPos` diagnostic line
  now includes the attempt count taken
  (`yaw_raw=N (attempts=K) pitch_raw=N (attempts=K)`). If all retries
  still fail (e.g. a genuine SCS0009 bus hang per #100), the firmware
  seeds `pitch_motion_.current_deg` with `BOOT_INIT_PITCH_DEG` (45°)
  instead of leaving the struct-default `current_deg=0`, so the
  subsequent boot-init `WriteHeadAngles(0, 45, 4000)` interpolation
  becomes a near-no-op rather than walking `WritePos` calls upward
  from `pos=620` (the lower mechanical end-stop) through
  end-stop-adjacent positions. The `BOOT_INIT_YAW_DEG` /
  `BOOT_INIT_PITCH_DEG` / `BOOT_INIT_MOVE_MS` constants are promoted
  from local block scope to class-level `static constexpr` so the
  safe-fallback branch can reference them. Closes
  [#138](https://github.com/kisaragi-mochi/stackchan-mcp/issues/138).
  Refs
  [#121](https://github.com/kisaragi-mochi/stackchan-mcp/issues/121)
  Problem 1.


- Fixed a missing trailing comma in
  `firmware/main/boards/freenove-esp32s3-display-2.8-lcd/config.json`
  that caused `release.py` to fail to parse the file and silently skip
  the `sdkconfig_append` entries for the
  `freenove-esp32s3-display-2.8-lcd` board variant. The variant builds
  now pick up `CONFIG_LANGUAGE_EN_US=y`,
  `CONFIG_SR_WN_WN9S_HIESP=y`, and `CONFIG_SR_WN_WN9_HIESP=y` as
  intended, and the spurious `[ERROR] Failed to parse ...` line is no
  longer printed during any `release.py` invocation. The default
  `release.py stackchan` build, which only targets the `stackchan`
  board, is unaffected. Closes
  [#113](https://github.com/kisaragi-mochi/stackchan-mcp/issues/113).

- `release.py` now masks `CONFIG_DEFAULT_WEBSOCKET_TOKEN` in its
  summary stdout so build logs are safe to paste into Issues / PRs
  without leaking a personal token. The token is still applied to
  the build itself; this change is purely build-log hygiene. Closes
  [#131](https://github.com/kisaragi-mochi/stackchan-mcp/issues/131).

## [0.7.0] - 2026-05-14

### Gateway

- `listen()` now accepts optional visual/motion feedback arguments:
  `motion="face-only"` shows the `thinking` avatar during capture and
  restores `idle` at the end, while `motion="look-up"` preserves yaw,
  tilts pitch to `look_up_pitch` (validated to 5..85 degrees), shows
  `thinking`, and holds the pose on success so the caller's response
  can continue from the attentive posture. The default
  `motion="none"` preserves the existing behavior. Refs #96.

- `move_head` MCP tool now constrains `pitch` to `5..85` — the
  M5Stack-recommended operating range. Both the `inputSchema`
  (`minimum: 5`, `maximum: 85` for `pitch`; `minimum: -90`, `maximum: 90`
  for `yaw`) and the gateway `call_tool` handler enforce the bound as
  belt-and-suspenders. The tool description now references
  `set_head_angles` for callers that genuinely need the wider firmware
  hard clamp (`0..88`). This also refuses `move_head(yaw=0, pitch=0)`
  and other below-`5°` pitch requests at the MCP boundary, so an
  LLM-driven agent cannot trigger the SCS0009 servo bus hang state
  tracked in
  [#100](https://github.com/kisaragi-mochi/stackchan-mcp/issues/100)
  from a default pose-reset call. See README "Y-axis (pitch) safe
  range — two-tier guard" for the gateway-side restrictive vs
  firmware-side permissive policy contrast, and the comment thread on
  [#99](https://github.com/kisaragi-mochi/stackchan-mcp/issues/99) /
  [#100](https://github.com/kisaragi-mochi/stackchan-mcp/issues/100)
  for the on-device reproduction (2026-05-14). Closes
  [#109](https://github.com/kisaragi-mochi/stackchan-mcp/issues/109).


## [firmware-v1.4.1] - 2026-05-14

### Firmware

- Fixed user-configured WebSocket gateway URLs (e.g.
  `ws://192.168.x.y:8765`) being silently overwritten on every boot by
  the upstream xiaozhi OTA-config response. `Ota::CheckVersion()` still
  runs (firmware-version / activation / server-time / MQTT paths are
  unchanged), but the `websocket` section of the response is no longer
  written back into NVS by default. A new Kconfig option
  `CONFIG_DISABLE_OTA_WEBSOCKET_CONFIG` (default `y`) gates this
  behavior; setting it to `n` restores the original
  xiaozhi-esp32 NVS overwrite path. The misleading comment in
  `WebsocketProtocol::OpenAudioChannelInternal()` that claimed the
  OTA-config path was already disabled has been corrected to reflect
  the actual gating. Closes
  [#110](https://github.com/kisaragi-mochi/stackchan-mcp/issues/110).

- The firmware now actively positions the head at a fall-safe neutral
  pose (`yaw=0°`, `pitch=45°`) at the end of `InitializeServo()`, before
  any MCP command can arrive. Previously the head retained whatever
  angle it was left at on power-down, which could include end-stop
  positions (e.g. `pitch=0°`) that triggered the SCS0009 bus hang
  documented in
  [#100](https://github.com/kisaragi-mochi/stackchan-mcp/issues/100)
  on the first user-driven motion. The new boot-time positioning uses
  the existing interpolating `WriteHeadAngles` path with a 1-second
  move duration plus a 100 ms settle delay, mirroring the `goHome()`
  pattern in `m5stack/StackChan` and the timing established in
  `mongonta0716/stackchan-arduino`. Existing pitch guards (`0..88`
  hard clamp / `5..85` recommended range) continue to apply
  unchanged. Implements
  [#99](https://github.com/kisaragi-mochi/stackchan-mcp/issues/99)
  Option C and the boot-init aspect of
  [#100](https://github.com/kisaragi-mochi/stackchan-mcp/issues/100)
  direction E. Refs
  [#115](https://github.com/kisaragi-mochi/stackchan-mcp/issues/115).

- Improved `get_head_angles` MCP-tool diagnostics so that transient
  `ReadPos` failures can be distinguished from a genuine SCS0009 bus
  hang. The handler now retries `ReadPos` up to three times (50 ms
  inter-attempt delay) per servo ID while holding `scs_bus_mutex_`
  across the whole sequence. The success-path JSON output
  (`{"yaw":N,"pitch":N}`) is unchanged; on persistent failure the tool
  now returns
  `{"yaw":null,"pitch":null,"error":"ReadPos failed ...","servo_ok":bool,"yaw_attempts":N,"pitch_attempts":N}`
  instead of the previous sentinel `{"yaw":-144,"pitch":-194}` (the
  `-1` return from `ReadPos` run through the same
  `(pos-zero) * 5 / 16` degree-conversion math as a valid raw position,
  which was indistinguishable from a hang at the MCP layer and
  contributed to the hang judgments recorded in
  [#1](https://github.com/kisaragi-mochi/stackchan-mcp/issues/1) /
  [#100](https://github.com/kisaragi-mochi/stackchan-mcp/issues/100) /
  [#118](https://github.com/kisaragi-mochi/stackchan-mcp/issues/118)).
  The serial-log line is also expanded to include `servo_ok`, raw
  `ReadPos` values, and attempt counts. As a further investigation
  aid, `InitializeServo()` now logs pre- and post-init `ReadPos` raw
  values and tick timestamps around the boot-init `WriteHeadAngles`
  call, making the "unintended downward drop on power-on" investigation
  ([#121](https://github.com/kisaragi-mochi/stackchan-mcp/issues/121))
  data-driven via the serial log. Refs
  [#123](https://github.com/kisaragi-mochi/stackchan-mcp/issues/123).

- The boot-time `WriteHeadAngles` interpolated move added in #115 /
  PR #117 now runs over 4 seconds instead of 1, dropping the
  effective angular speed for the post-power-on climb to the
  fall-safe neutral pose from approximately 45°/s to approximately
  11°/s. On-device feedback identified the original 1-second
  duration as startling ("ブルンっ" / audible servo stress) on the
  CoreS3 + SCS0009 hardware. The move is otherwise unchanged — same
  target (`yaw=0°`, `pitch=45°`), same path through
  `WriteHeadAngles` / the `servo_motion` task, same 100 ms
  post-settle vTaskDelay margin (so total boot-init now takes about
  4.1 seconds instead of 1.1 seconds before the first MCP command
  can arrive). Refs
  [#121](https://github.com/kisaragi-mochi/stackchan-mcp/issues/121)
  Problem 2 (climb speed); the separate "unintended downward drop on
  power-on" (#121 Problem 1 / hypotheses 1–3) remains under
  investigation and is unaffected by this change.

## [0.6.0] - 2026-05-12

### Added

- Phase 4 STT — gateway-side `listen(duration_ms?, engine?, language?,
  model?)` MCP tool. The gateway puts the device firmware into
  listening mode over the existing WebSocket, buffers the Opus frames
  the device streams up during the capture window, then decodes and
  transcribes them through a registered STT engine. The default
  engine is **faster-whisper** (local, MIT-licensed, runs on CPU);
  the **OpenAI Whisper API** is available as an alternative for
  setups without local compute. Install with
  `pip install stackchan-mcp[stt-faster-whisper]` (or `[stt-openai]`)
  — the `[stt]` base extra pulls in `opuslib` for inbound frame
  decoding. The framework is engine-agnostic (`stt.STTEngine` ABC +
  `stt.EngineRegistry`, symmetric to the existing `tts` package), so
  additional engines can be added in follow-up PRs without touching
  the orchestration pipeline. Configure faster-whisper with
  `STACKCHAN_FASTER_WHISPER_MODEL` (default `base`), `_DEVICE`
  (default `cpu`), and `_COMPUTE_TYPE` (default `int8`); the OpenAI
  engine reads `OPENAI_API_KEY` plus the optional
  `STACKCHAN_OPENAI_WHISPER_MODEL` (default `whisper-1`). Requires a
  paired firmware update — see the Firmware section below. Refs #91.

### Firmware

> The firmware changes below were released through the dedicated firmware
> release stream as `firmware-v1.0.0` (2026-05-10), `firmware-v1.1.0`
> (2026-05-10), `firmware-v1.2.0` (2026-05-11), and `firmware-v1.3.0`
> (paired with this gateway release — contains the server-driven
> listening trigger that the new `listen()` MCP tool depends on).
> Prebuilt binaries (`merged-binary.bin` / `xiaozhi.bin` /
> `v*_stackchan.zip`) for each tag are attached to the corresponding
> GitHub release:
> https://github.com/kisaragi-mochi/stackchan-mcp/releases.
> PyPI users running pre-v1.3.0 firmware can still upgrade to
> `stackchan-mcp` 0.6.0 — only the new `listen()` MCP tool requires the
> paired firmware update; the existing `say()` and other tools continue
> to work against older firmware.

- **Server-driven listening trigger** (paired with the new
  `listen()` MCP tool above, Issue #91). The firmware's
  `Application::OnIncomingJson` handler now accepts inbound
  `{"type":"listen","state":"start"|"stop"}` messages from the
  gateway and dispatches them to `Application::StartListening` /
  `StopListening`. The wire format mirrors the existing device→gateway
  `Protocol::SendStartListening` notification in the reverse
  direction; the upstream 78/xiaozhi-esp32 protocol has no inbound
  listen type today, so this extension is additive and does not
  collide with anything upstream. The `mode` field is parsed on
  `state="start"` but currently ignored — `HandleStartListeningEvent`
  unconditionally enters `kListeningModeManualStop`, which is also
  the right behaviour for gateway-driven capture (the gateway
  controls the stop boundary explicitly). Threading `auto` /
  `realtime` mode through is a follow-up. Refs #91.
- **TTS lip-sync (state-driven)**: drive avatar mouth animation while
  the gateway is speaking. The firmware now reacts to the
  `tts.start` / `tts.stop` JSON notifications introduced in #75 (Issue
  #70 PR2) and cycles the mouth shape through `closed → half → open →
  half` on a fixed 150 ms cadence for the lifetime of each utterance,
  snapping back to `closed` at stop. Autonomous blink is paused while
  active (same Phase 2 trade-off as the existing `set_mouth_sequence`
  task: a blink ending would otherwise restore the full-face image
  and overwrite the mouth overlay) and restored at stop based on
  `blink_desired_` so a `set_blink` issued mid-playback is honoured.
  Coexists with user-issued `set_mouth` / `set_mouth_sequence` calls
  by yielding the current frame when `mouth_seq_active_` is true; the
  user-issued sequence wins until it completes, then lip-sync resumes
  on the next tick. Wired through a new no-op `Board::OnTtsStart` /
  `OnTtsStop` hook so non-stackchan boards are unaffected. The (B)
  audio-envelope-driven follow-up proposed in the issue will be
  tracked separately. Closes #76. Refs #70, #75.
- **Default servo driver switched to MIT FeetechScs** (Phase A of the
  GPL → MIT firmware migration tracked in #79). The opt-in MIT driver
  added in #82 is now the build default for the canonical build path
  (`firmware/scripts/release.py stackchan`), which now appends
  `CONFIG_STACKCHAN_SERVO_FEETECH=y` to the per-board `sdkconfig` so
  the selection is enforced regardless of any pre-existing
  `firmware/sdkconfig` left in the workspace. Builds produced this way
  exclude the GPL-3.0 SCServo_lib sources from the linked binary —
  i.e. the firmware binary is MIT-licensed end-to-end. Equivalence
  with SCSCL was validated on M5Stack CoreS3 + SCS0009 x2 in #82.

  **Migration note for users with an existing `firmware/sdkconfig`:**
  ESP-IDF persists Kconfig choices into `firmware/sdkconfig`, and a
  Kconfig `default` change does not retroactively rewrite that file.
  This release works around that by enforcing the choice at the
  `release.py` layer (`sdkconfig_append`); rebuilding via
  `release.py stackchan` produces an MIT default binary even on a
  workspace that previously selected SCSCL. If you build by directly
  invoking `idf.py` instead and have an existing `sdkconfig`, you may
  need to run `idf.py menuconfig` (or delete `firmware/sdkconfig`) to
  pick up the new default.

  **Opting back into the GPL fallback:** the GPL-3.0 SCServo_lib
  sources remain in-tree. To build against them, add
  `CONFIG_STACKCHAN_SERVO_SCSCL=y` to `firmware/sdkconfig.defaults.local`
  (which `release.py` merges in *after* `sdkconfig_append`, so it wins
  over the FEETECH default).

  Phase B (removal of the SCServo_lib sources, fully MIT-only
  firmware) is gated on a multi-week observation period in #79 closing
  without regressions. Please open an issue referencing #79 if you
  observe any regression after rebuilding so the migration can be held
  or rolled back. Refs #79.
- `firmware/scripts/release.py` no longer **skips** the build when
  `releases/v{PROJECT_VER}_{board}.zip` already exists. `PROJECT_VER`
  is pinned to the upstream xiaozhi-esp32 firmware version
  (currently `2.2.6`) and is not bumped per stackchan-mcp change, so
  the previous skip behaviour silently let an old artifact survive a
  rebuild invocation — including across license-sensitive
  configuration changes such as the FEETECH/SCSCL servo driver swap
  above. Existing `releases/v2.2.6_<board>.zip` files are now
  automatically replaced by a fresh build (`zip_bin` already unlinks
  before writing). Manual `rm -rf firmware/releases/v2.2.6_*.zip`
  before invoking `release.py` is no longer required. Refs #79.
- Add opt-in MIT-licensed servo driver alternative
  (`firmware/components/feetech_scs/`, vendored from
  [necobit/feetech_scs_esp_idf](https://github.com/necobit/feetech_scs_esp_idf)
  at commit `38a91984`). Build with Kconfig
  `CONFIG_STACKCHAN_SERVO_FEETECH=y` to exclude the GPL-3.0 SCServo_lib
  sources and produce a fully MIT-licensed firmware binary. (Promoted
  to default in the entry above.) Refs #79.
- **Hardware safety**: clamp `set_head_angles` pitch parameter to a
  hardware-safe sub-range (`0..+30°`) to prevent driving the SCS0009 servo
  into its mechanical end-stop. M5Stack docs explicitly warn that operating
  the Y-axis outside the recommended range may cause servo stall and
  permanent damage; on the CoreS3 + SCS0009 hardware this firmware targets,
  the mechanical end-stop sits at approximately `pitch=-1°` (validated on
  a real unit during #79). The MCP property declaration keeps the
  `-30..+30°` numerical range for backward compatibility, but the handler
  silently raises sub-zero requests with an `ESP_LOGW` warning. README
  gains a new "Hardware safety notes" section. Refs #80.


## [0.5.0] - 2026-05-10

### Added

- Phase 4 TTS — gateway-side `say(text, voice?, speaker_id?,
  reference_audio?)` MCP tool. The gateway synthesises speech via a
  registered TTS engine, encodes the result to Opus (16 kHz mono,
  60 ms frames), and pushes the frames to the device over the existing
  WebSocket binary channel. **No firmware changes are required**: the
  device's WebSocket protocol already accepts Opus payloads as binary
  frames. The default engine is **VOICEVOX**, which runs as a separate
  HTTP service (the official `voicevox/voicevox_engine` Docker image is
  the recommended setup), so VOICEVOX's LGPL-3.0 license stays scoped
  to the engine process and does not affect the gateway. Install with
  `pip install stackchan-mcp[tts]` (adds `httpx` and `opuslib`); the
  `[tts-voicevox]` extra is provided as an intent-declaring alias.
  Configure with `STACKCHAN_VOICEVOX_URL` (default
  `http://127.0.0.1:50021`) and `STACKCHAN_VOICEVOX_DEFAULT_SPEAKER`
  (default `3`, Zundamon normal). The framework is engine-agnostic
  (`tts.TTSEngine` ABC + `tts.EngineRegistry`), so additional engines —
  e.g. Irodori-TTS for zero-shot voice cloning — can be added in
  follow-up PRs without touching the orchestration pipeline. Refs #70.
- New MCP tools to drive the 12× WS2812C RGB LEDs on the StackChan
  base: `set_led(index, r, g, b)`, `set_all_leds(r, g, b)`,
  `set_leds(colors)` (batch, single I2C burst for animations), and
  `clear_leds()`. The strip is wired to the PY32L020 IO expander on
  expander pin 13 — not an ESP32 GPIO — so the firmware extends the
  existing `Py32IoExpander` helper with `SetDriveMode`, `SetLedCount`,
  `SetLedColor` (RGB888 → RGB565 packing), `SetLedData` (burst write),
  and `RefreshLeds` (RMW preserves the count nibble alongside the
  bit-6 latch trigger), matching the M5 BSP's
  `PY32IOExpander_Class.cpp`. `InitializeIOExpander` configures pin 13
  as push-pull output with pull-up, sets the LED count to 12, waits
  the M5-prescribed 200 ms before the first refresh, then clears the
  strip. The on-device tools (`self.led.set_color`, `self.led.set_all`,
  `self.led.set_many`, `self.led.clear`) all latch implicitly so each
  call is WYSIWYG; the gateway re-packs the Python `colors` list as a
  JSON string for `set_many` since the device-side MCP property layer
  has only scalar types. `set_many` validates every entry (including
  a `cJSON_IsNumber` guard so non-number elements like `"255"` or
  `null` no longer silently coerce to 0) before any I2C write, so
  malformed input cannot leave the strip in a partially-mutated
  state. All four tools no-op cleanly with an `available=false`
  reply when the PY32 init failed, so a flaky expander does not
  cascade into errors.


## [0.4.0] - 2026-05-09

### Firmware

- Fixed: WebSocket auto-reconnect now triggers on any post-handshake
  server- or network-initiated disconnect — gateway crashes, TLS-layer
  resets, and gateway configurations that tear the WebSocket session
  down after the handshake. Previously the firmware logged the
  disconnect, returned to `idle`, and stayed there until a hard reset
  or user interaction; the reconnect path introduced in PR #35 was
  effectively suppressed for these cases. Real-device tracing (CoreS3,
  TLS-terminated gateway) showed that the original global atomic
  `auto_reconnect_enabled_` flag was being cleared by an *unrelated*
  user-initiated path (`HandleToggleChatEvent → CloseAudioChannel`,
  reachable via a brief tap on the FT6336 LCD touch panel while the
  device was in `listening`) running on the main task between handshake
  completion and the `OnDisconnected` lambda firing on the WS task —
  silently disarming the reconnect even when the underlying close was
  not user-initiated. The fix replaces the global atomic flag with a
  per-socket `shared_ptr<std::atomic<bool>>` (`notify_disconnect`)
  whose lifetime is tied to the websocket itself: each candidate in
  `OpenAudioChannelInternal()` creates its own token, `ParseServerHello`
  flips it to `true` the moment the server hello arrives (before
  setting the wait bit, so a near-simultaneous close still observes an
  armed flag), and any path that intentionally tears down *that
  specific* socket (`CloseAudioChannel`, the destructive prologue of
  `OpenAudioChannelInternal`, or the destructor) flips it back to
  `false` synchronously before invoking `websocket_.reset()`. The
  lambda's early-return guard short-circuits if and only if the
  firmware itself wanted the close, while every server- or
  network-initiated disconnect falls through to `ScheduleReconnect()`.
  A separate `intentional_close_` atomic re-checks intent on the main
  task right before the deferred reconnect job runs, so a reconnect
  enqueued via `Application::Schedule()` before `CloseAudioChannel()`
  ran cannot reopen the channel against the user's intent (the
  `esp_timer_stop()` call alone cannot cancel work the timer has
  already re-posted). The `esp_timer_create` and `esp_timer_start_once`
  return values are now inspected and logged on failure, and
  `esp_timer_stop()` warnings other than `ESP_ERR_INVALID_STATE` are
  surfaced. ([#61])

[#61]: https://github.com/kisaragi-mochi/stackchan-mcp/issues/61

### Added

- New `set_mouth_sequence` MCP tool: queue a list of
  `{shape, duration_ms}` steps and play it on the device locally so a
  TTS-driven caller can ship one MCP call per utterance instead of N
  back-to-back `set_mouth` calls (which suffer per-step WebSocket RTT
  jitter). The gateway exposes a proper array schema with `shape`
  constrained to `closed | half | open | e | u` and `duration_ms` to
  10..10000 ms; sequences are 1..256 steps. Because the ESP32 MCP
  Property type system only supports string/integer/boolean, the
  gateway serialises `steps` to a JSON string under `steps_json` and
  the firmware decodes it via cJSON; this is an internal wire detail
  hidden from MCP clients. Validation is atomic — if any step is
  malformed the whole call is rejected and nothing is queued (no
  half-played sequences). Calling `set_mouth`, `set_avatar`, or
  `set_mouth_sequence` again interrupts the in-flight sequence and
  also clears any pending-but-not-yet-started sequence, so a
  `set_mouth("closed")` issued in the brief window between
  `set_mouth_sequence` returning and the playback task waking up
  still wins. Each preempt also bumps a generation token that the
  playback task re-checks before every frame draw, eliminating the
  small race where a stale mouth frame could otherwise be drawn
  after a newer command had already returned to the caller. A
  separate `cancel_mouth_sequence` tool is intentionally not added —
  `set_mouth("closed")` doubles as the cancellation path, keeping
  the MCP surface minimal. Autonomous blink is paused during
  playback because the blink state machine ends by restoring the
  last full-face image, which would otherwise overwrite the active
  mouth overlay; blink is resumed when the sequence finishes (or is
  interrupted) by reading the user's most recent `set_blink` intent
  rather than a snapshot taken at sequence start, so a `set_blink`
  call issued mid-sequence is honoured (such a call returns `ok`
  with `deferred: true` and applies the moment the sequence ends).
  Note: like `set_mouth`, the final mouth shape can also be replaced
  by the resting face once an autonomous blink fires after the
  sequence — this is the same Phase 2 trade-off (the blink state
  machine ends by repainting the full face). Callers that need a
  non-closed final shape to persist visually should disable blink
  with `set_blink(false)` before the sequence; a follow-up to make
  blink composable with mouth overlays is tracked separately. The final shape is held after the sequence finishes
  so callers can compose with future expression-style use cases;
  append a `{"shape": "closed", "duration_ms": ...}` step if you want
  the mouth to close at the end. Designed to compose with a future
  TTS pipeline (caller pre-computes shapes from phonemes / aeneas
  alignment / mora timing and ships the whole sequence in one call).
  Requires a firmware update. ([#5])
- The on-device WiFi configuration UI now also exposes a **Fallback
  Gateway URL** field and a **Gateway Token** field on the **Advanced**
  tab, alongside the existing WebSocket Gateway URL field. The values
  are persisted to the `websocket` NVS namespace as
  `websocket.fallback_url` and `websocket.token` — the same keys the
  firmware connection logic reads on the next boot. End users running a
  pre-built firmware can now configure the full primary + fallback +
  bearer-token gateway profile from `http://192.168.4.1` without
  rebuilding from source. Token handling is hardened against the
  unauthenticated WiFi config AP: the token value is never returned by
  the configuration GET endpoint (only an "is set" boolean), is
  rendered as a password input, and is redacted from the per-submit
  save log. Submitting the form with the token field left blank keeps
  the existing token; typing a new value updates it; ❌ writes an empty
  string to NVS so the firmware falls back to the build-time
  `CONFIG_DEFAULT_WEBSOCKET_TOKEN` on the next boot — which disables
  auth on stock builds where no Kconfig default is set, but reverts to
  the bundled default on builds that ship one. ([#43])

[#5]: https://github.com/kisaragi-mochi/stackchan-mcp/issues/5
[#43]: https://github.com/kisaragi-mochi/stackchan-mcp/issues/43


## [0.3.0] - 2026-05-08

### Added

- `stackchan-mcp` CLI now supports `--help` / `-h` and `--version` /
  `-V` flags. `--help` prints usage and the supported environment
  variables (`STACKCHAN_TOKEN`, `VISION_URL`, `VISION_HOST`,
  `VISION_TOKEN`, `HOST`, `WS_PORT`, `CAPTURE_PORT`) plus pointers to
  the in-tree READMEs, and exits without binding any ports.
  `--version` prints the installed package version. End users running
  `pipx install stackchan-mcp` can now confirm the install and check
  basic usage without starting a server. ([#52], [#53])
- `stackchan-mcp --check` runs a non-destructive preflight and exits
  without entering the stdio MCP loop. It loads `.env`, reports
  configuration with secrets redacted (`STACKCHAN_TOKEN`, `VISION_URL`
  derived from `VISION_HOST` when not set explicitly, `VISION_TOKEN`),
  probes the WebSocket and capture ports (`HOST:WS_PORT`,
  `HOST:CAPTURE_PORT`) via a non-blocking `bind()`, and best-effort
  reports the holding process via `lsof` when a port is in use. Exit
  status is `0` if ready, non-zero with an issue count when at least
  one blocking problem is detected. Live device connectivity probing
  is intentionally out of scope. ([#54])

### Changed

- `stackchan_mcp.__version__` is now resolved from installed package
  metadata (`importlib.metadata.version("stackchan-mcp")`) instead of
  a hard-coded literal, so the value tracks `gateway/pyproject.toml`
  automatically across releases.

[#52]: https://github.com/kisaragi-mochi/stackchan-mcp/issues/52
[#53]: https://github.com/kisaragi-mochi/stackchan-mcp/issues/53
[#54]: https://github.com/kisaragi-mochi/stackchan-mcp/issues/54


## [0.2.0] - 2026-05-08

### Added

- `set_avatar` now accepts `"off"` as a face value. When called with
  `"off"`, the avatar layer is hidden and autonomous blinking is
  disabled so the underlying xiaozhi-esp32 screens (WiFi config UI,
  OTA, settings) become visible on the LCD without erasing NVS.
  Calling `set_avatar` with any other face brings the avatar back and
  restores the previous blink state automatically. ([#3])
- The on-device WiFi configuration UI now exposes a **WebSocket
  Gateway URL** field on the **Advanced** tab. The value is persisted
  to the `websocket` NVS namespace (`websocket.url`), which is the
  same key the firmware connection logic reads on the next boot. End
  users running a pre-built firmware can now point a fresh device at
  their stackchan-mcp gateway from `http://192.168.4.1` without
  rebuilding from source. The upstream `78/esp-wifi-connect` managed
  component is kept in `firmware/components/78__esp-wifi-connect/` as
  a project-level component override so the patch is explicit and
  versioned in this repository. ([#25])

[#3]: https://github.com/kisaragi-mochi/stackchan-mcp/issues/3
[#25]: https://github.com/kisaragi-mochi/stackchan-mcp/issues/25


## [0.1.0] - 2026-05-07

Initial PyPI release of the gateway. End users can now install the MCP
server without cloning the monorepo:

```bash
pipx install stackchan-mcp
# or
uv tool install stackchan-mcp
```

### Added

- Publish the gateway to PyPI as `stackchan-mcp`. A single
  `stackchan-mcp` console script starts the gateway. ([#11], [#46])
- Tag-driven publish workflow (`.github/workflows/publish.yml`) using
  PyPI Trusted Publishing. The workflow refuses to publish unless the
  tag commit is on `origin/main`, the tag has a `v` prefix and matches
  `gateway/pyproject.toml` after PEP 440 normalization, the version is
  not a PEP 440 local version, and `uv run ruff check .` plus
  `uv run pytest` succeed inside `gateway/`. ([#46])
- `workflow_dispatch` dry-run support for `publish.yml` so maintainers
  can verify lint, test, and build without cutting a tag; the publish
  job is gated on `push` so manual runs cannot release. ([#47])
- Bundle the MIT `LICENSE` in both the published wheel and sdist via
  `license-files = ["LICENSE"]` (PEP 639). ([#46])

### Changed

- Split the gateway entry point into `stackchan_mcp.cli:main` so that
  `import stackchan_mcp` is side-effect-free (no `load_dotenv()` or
  `logging.basicConfig()` at import time). `python -m stackchan_mcp`
  continues to work through a thin re-export in `__main__.py`. ([#46])

### Fixed

- Pin `astral-sh/setup-uv` to a full `vX.Y.Z` tag (`v8.1.0`) in the
  publish workflow. Starting with v8 the upstream ships immutable
  releases only and does not maintain a moving `@v8` major-version
  alias, so the previous floating pin no longer resolved. ([#47])


[Unreleased]: https://github.com/kisaragi-mochi/stackchan-mcp/compare/firmware-v1.8.0...HEAD
[firmware-v1.8.0]: https://github.com/kisaragi-mochi/stackchan-mcp/compare/firmware-v1.7.0...firmware-v1.8.0
[0.8.0]: https://github.com/kisaragi-mochi/stackchan-mcp/compare/v0.7.0...v0.8.0
[firmware-v1.7.0]: https://github.com/kisaragi-mochi/stackchan-mcp/compare/firmware-v1.6.0...firmware-v1.7.0
[firmware-v1.6.0]: https://github.com/kisaragi-mochi/stackchan-mcp/compare/firmware-v1.5.0...firmware-v1.6.0
[firmware-v1.5.0]: https://github.com/kisaragi-mochi/stackchan-mcp/compare/firmware-v1.4.1...firmware-v1.5.0
[0.7.0]: https://github.com/kisaragi-mochi/stackchan-mcp/compare/v0.6.0...v0.7.0
[firmware-v1.4.1]: https://github.com/kisaragi-mochi/stackchan-mcp/compare/firmware-v1.3.0...firmware-v1.4.1
[0.6.0]: https://github.com/kisaragi-mochi/stackchan-mcp/compare/v0.5.0...v0.6.0
[firmware-v1.3.0]: https://github.com/kisaragi-mochi/stackchan-mcp/compare/firmware-v1.2.0...firmware-v1.3.0
[firmware-v1.2.0]: https://github.com/kisaragi-mochi/stackchan-mcp/compare/firmware-v1.1.0...firmware-v1.2.0
[firmware-v1.1.0]: https://github.com/kisaragi-mochi/stackchan-mcp/compare/firmware-v1.0.0...firmware-v1.1.0
[firmware-v1.0.0]: https://github.com/kisaragi-mochi/stackchan-mcp/releases/tag/firmware-v1.0.0
[0.5.0]: https://github.com/kisaragi-mochi/stackchan-mcp/releases/tag/v0.5.0
[0.4.0]: https://github.com/kisaragi-mochi/stackchan-mcp/releases/tag/v0.4.0
[0.3.0]: https://github.com/kisaragi-mochi/stackchan-mcp/releases/tag/v0.3.0
[0.2.0]: https://github.com/kisaragi-mochi/stackchan-mcp/releases/tag/v0.2.0
[0.1.0]: https://github.com/kisaragi-mochi/stackchan-mcp/releases/tag/v0.1.0

[#11]: https://github.com/kisaragi-mochi/stackchan-mcp/issues/11
[#46]: https://github.com/kisaragi-mochi/stackchan-mcp/pull/46
[#47]: https://github.com/kisaragi-mochi/stackchan-mcp/pull/47
