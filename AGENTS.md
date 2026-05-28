# stackchan-mcp

MCP (Model Context Protocol) gateway and ESP32 firmware for **stack-chan**, the super-kawaii communication robot originated by [Shinya Ishikawa](https://github.com/stack-chan/stack-chan) in 2021.

## Review guidelines

These guidelines apply to all automated and human code reviews in this repository.

- **User-first, not infra-first.** This is a single-user hobby product running on a home LAN. A finding that matters for a 10k-RPS SaaS is noise here. Focus on: does the feature work for the person sitting next to the robot?
- **Main path correctness over edge-case hardening.** If the happy path is broken, that is P0. If a rare race condition could theoretically cause a retry, that is P2 at most.
- **DoS / resource-exhaustion findings stay low priority.** There is one user, one device, one LAN. Denial-of-service is not a meaningful threat model. Do not escalate these to P0/P1.
- **Be kind to contributors.** This project welcomes first-time and hobbyist contributors. Frame review comments as suggestions, not demands. Explain *why* a change matters, not just *what* to change. If a finding is stylistic rather than functional, mark it explicitly as non-blocking.
- **Respect the existing architecture.** The gateway (Python, stdio MCP) and firmware (ESP32, C++) are intentionally separate processes communicating over WebSocket. Do not suggest merging them or adding cross-layer coupling unless there is a concrete functional reason.

## Repository structure

```
stackchan-mcp/
  gateway/          Python MCP gateway (stdio server, PyPI: stackchan-mcp)
  firmware/         ESP32 firmware (xiaozhi-esp32 fork, stackchan board)
    main/
      boards/
        stackchan/  Board-specific code (servo, touch, avatar, audio)
    scripts/        Build helpers (release.py, avatar_convert/)
  docs/             Additional documentation
  CONTRIBUTING.md   Contribution guide, license boundary, release steps
  CHANGELOG.md      Release history
  LICENSE           MIT (see CONTRIBUTING.md for GPL-3.0 island)
```

## Getting started

| Task | Where to look | Key commands |
|---|---|---|
| Install the gateway | `README.md` Quick Start | `pipx install stackchan-mcp` |
| Build firmware | `firmware/AGENTS.md` | `docker run ... python ./scripts/release.py stackchan` |
| Flash firmware | `firmware/AGENTS.md` | `esptool.py ... write_flash 0x20000 build/xiaozhi.bin` |
| Run gateway tests | `gateway/AGENTS.md` | `cd gateway && uv run pytest && uv run ruff check .` |
| Understand the board | `firmware/main/boards/stackchan/AGENTS.md` | Hardware specs, servo/touch/avatar details |
| Prepare for release | `CONTRIBUTING.md` Per-release steps | Version bump + CHANGELOG promote + tag push |

## Dual-language documentation

`README.md` (English) and `README.ja.md` (Japanese) must stay in sync. When changing one, update the other in the same commit.

## License boundary

The repository is MIT-licensed, with one exception: 8 files under `firmware/main/boards/stackchan/` originating from SCServo_lib are GPL-3.0. See `CONTRIBUTING.md` for the full list and boundary rules. The gateway (Python) is a separate process and is not affected by the GPL island.
