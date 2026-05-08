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

### Added

- `stackchan-mcp` CLI now supports `--help` / `-h` and `--version` /
  `-V` flags. `--help` prints usage and the supported environment
  variables (`STACKCHAN_TOKEN`, `VISION_URL`, `VISION_HOST`,
  `VISION_TOKEN`, `HOST`, `WS_PORT`, `CAPTURE_PORT`) plus pointers to
  the in-tree READMEs, and exits without binding any ports.
  `--version` prints the installed package version. End users running
  `pipx install stackchan-mcp` can now confirm the install and check
  basic usage without starting a server. ([#52], [#53])

### Changed

- `stackchan_mcp.__version__` is now resolved from installed package
  metadata (`importlib.metadata.version("stackchan-mcp")`) instead of
  a hard-coded literal, so the value tracks `gateway/pyproject.toml`
  automatically across releases.

[#52]: https://github.com/kisaragi-mochi/stackchan-mcp/issues/52
[#53]: https://github.com/kisaragi-mochi/stackchan-mcp/issues/53

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

[Unreleased]: https://github.com/kisaragi-mochi/stackchan-mcp/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/kisaragi-mochi/stackchan-mcp/releases/tag/v0.2.0
[0.1.0]: https://github.com/kisaragi-mochi/stackchan-mcp/releases/tag/v0.1.0

[#11]: https://github.com/kisaragi-mochi/stackchan-mcp/issues/11
[#46]: https://github.com/kisaragi-mochi/stackchan-mcp/pull/46
[#47]: https://github.com/kisaragi-mochi/stackchan-mcp/pull/47
