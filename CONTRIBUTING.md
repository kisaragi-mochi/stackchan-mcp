# Contributing

Thanks for helping improve `stackchan-mcp`.

This repository contains both the Python MCP gateway and the ESP32 firmware
used by the M5Stack official StackChan kit. The goal is to keep the public
project easy to reproduce: issues, pull requests, commits, and documentation
should contain technical context that is useful to other builders.

## Setup

Install the tools needed for the part of the repository you are changing:

- Firmware: Docker or a compatible container runtime that can run
  `espressif/idf:v5.5.2`
- Gateway: Python managed with `uv`
- Hardware testing: M5Stack CoreS3 + official StackChan kit

For gateway development:

```bash
cd gateway
uv sync
```

For firmware development, use the board-aware release script shown below. It
sets the StackChan board configuration before building.

## Development Flow

For most changes, work on a topic branch:

```bash
git switch main
git pull
git switch -c issue-123-short-description
# edit files
git status
git diff
```

Then run the relevant checks, commit, push the branch, and open a pull request.
Link the related issue in the PR body when there is one.

## Pull Requests

Use the pull request template in `.github/PULL_REQUEST_TEMPLATE.md`. It is a
guide, not a gate. Fill in what you know, leave boxes unchecked when they do
not apply, and explain anything you could not test.

- Summary: what changed and why
- Test plan: checks that passed, plus any checks intentionally skipped
- Hardware notes: required for firmware changes
- Breaking changes: MCP tool API, NVS schema, build flags, or `None`
- Related issues: `Closes #N` or `Refs #N` when applicable

Small, focused PRs are easier to review. Prefer one issue per PR, or one small
maintenance purpose per PR.

## CI

The build workflow in `.github/workflows/build.yml` runs on pull requests and
pushes to `main`. It currently verifies:

- Firmware: `python ./scripts/release.py stackchan` inside
  `espressif/idf:v5.5.2`
- Gateway: `uv sync --frozen`, `uv run ruff check .`, and `uv run pytest`

CI is the shared baseline. For firmware changes, real hardware testing is still
needed before merge, but contributors without hardware are welcome to open PRs
and ask for maintainer verification.

## Gateway Checks

Run these for gateway changes:

```bash
cd gateway
uv sync
uv run pytest
uv run ruff check .
```

Add tests under `gateway/tests/` for behavior changes.

## Firmware Checks

Build the StackChan firmware through the board-aware release script:

```bash
cd firmware
docker run --rm -v "$PWD":/project -w /project espressif/idf:v5.5.2 \
  python ./scripts/release.py stackchan
```

This produces `build/merged-binary.bin` and
`releases/v2.2.6_stackchan.zip`.

Avoid using a plain `idf.py build` as proof that the StackChan target works; it
may build a different board configuration.

## Hardware Test Requirement

Firmware changes should be flashed to and verified on real StackChan hardware
before merge. Building without flashing is useful, but it is not sufficient as
the final verification for firmware behavior changes.

If you do not have hardware, you can still open a PR. Mark the Hardware section
as not available and describe the code-level checks you did run. A maintainer
can help decide whether to verify it on a device, keep it as a draft, or split
out a smaller change.

In the PR template's Hardware section, document what you verified. At minimum:

- Device boots without crash
- Existing MCP tools still work for the affected area
- New firmware behavior is tested on the real device

Gateway-only and documentation-only PRs do not require hardware testing. Mark
the Hardware section as not applicable.

## Do Not Commit Local Secrets

Please keep private or local machine state out of commits:

- `.env` files
- tokens, passwords, WiFi credentials
- private LAN IP addresses
- captured photos or user media
- local `firmware/sdkconfig`
- temporary build settings that force a personal gateway URL/token

Use placeholder examples in documentation instead. Firmware developers can put
personal Kconfig overrides in `firmware/sdkconfig.defaults.local`; it is ignored
by git and loaded by the firmware build.

## License Boundary

Most of this repository is MIT licensed. The SCServo-lib-derived files under
`firmware/main/boards/stackchan/` are GPL-3.0:

- `INST.h`
- `SCS.cc`
- `SCS.h`
- `SCSCL.cc`
- `SCSCL.h`
- `SCSerial.cc`
- `SCSerial.h`
- `SCServo.h`

Preserve the existing GPL-3.0 license headers in those files. Avoid moving
GPL-derived code into unrelated MIT-only areas, and do not include MIT-only
project headers from outside `firmware/main/boards/stackchan/` into the
GPL-derived files unless the licensing impact has been reviewed.

The gateway runs as an independent Python process and communicates with the
firmware over WebSocket, so the process boundary keeps the gateway side usable
under the MIT license.

## Review Process

Maintainer review is required before merge. Squash merge is preferred so each
PR lands as one coherent change on `main`.

Significant firmware changes should receive especially careful review for race
conditions, resource lifetime, boot behavior, NVS compatibility, and hardware
failure modes.

## Communication

Issue and PR descriptions should use English as the baseline. Japanese mixed
in is fine where it helps the StackChan community, but public technical context
should remain understandable to international contributors. Code comments
should be in English.

Be polite and concrete. This is a small hardware community, and clear technical
notes help the next person reproduce what happened.
