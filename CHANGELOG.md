# Changelog

All notable changes to this repository are documented here.

The format is based on
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Version tags (`vX.Y.Z`) track the **Python MCP gateway** published to PyPI
as `stackchan-mcp`. The ESP32 firmware lives in the same repository but is
built and distributed separately through `firmware/scripts/release.py` and
the upstream xiaozhi-esp32 firmware version (currently `v2.2.6`); when a
tagged gateway release also requires a coordinated firmware change, that
change is called out under a `Firmware` subsection of the release entry.

## [Unreleased]

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

[Unreleased]: https://github.com/kisaragi-mochi/stackchan-mcp/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/kisaragi-mochi/stackchan-mcp/releases/tag/v0.3.0
[0.2.0]: https://github.com/kisaragi-mochi/stackchan-mcp/releases/tag/v0.2.0
[0.1.0]: https://github.com/kisaragi-mochi/stackchan-mcp/releases/tag/v0.1.0

[#11]: https://github.com/kisaragi-mochi/stackchan-mcp/issues/11
[#46]: https://github.com/kisaragi-mochi/stackchan-mcp/pull/46
[#47]: https://github.com/kisaragi-mochi/stackchan-mcp/pull/47
