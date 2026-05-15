# Firmware Upstream Sync

This document describes how this repository tracks upstream `xiaozhi-esp32`
while keeping the StackChan-specific board support reviewable and reproducible.

## Repository Roles

There are three repositories involved:

| Repository | Role |
|---|---|
| [`78/xiaozhi-esp32`](https://github.com/78/xiaozhi-esp32) | Upstream firmware project |
| [`kisaragi-mochi/xiaozhi-esp32`](https://github.com/kisaragi-mochi/xiaozhi-esp32) | Maintainer fork used to absorb upstream changes and keep StackChan board support building |
| [`kisaragi-mochi/stackchan-mcp`](https://github.com/kisaragi-mochi/stackchan-mcp) | Public monorepo; imports the fork into `firmware/` as a git subtree |

The `stackchan-mcp` repository should pull firmware changes from the maintainer
fork, not directly from upstream. That keeps conflict resolution and firmware
validation in the firmware fork before the monorepo subtree changes.

## Sync Cadence

Use a quarterly upstream sync as the default cadence.

Do an earlier sync when:

- upstream contains a security fix or important ESP-IDF compatibility fix
- the StackChan board needs an upstream feature
- a release branch or prebuilt firmware release needs a newer upstream base

Stay pinned when:

- upstream churn conflicts heavily with StackChan board files
- the fork does not build for `stackchan`
- hardware behavior cannot be verified before a release

## Conflict Policy

Prefer adapting StackChan files to upstream when the upstream change is clearly
the new project direction. Stay pinned and document the reason when the change
would require a larger board rewrite or hardware retest.

Keep these boundaries in mind:

- Preserve GPL-3.0 headers in `firmware/main/boards/stackchan/` files derived
  from SCServo_lib.
- Do not commit local gateway URLs, LAN IP addresses, WiFi credentials, tokens,
  `.env` files, captured photos, or `firmware/sdkconfig.defaults.local`.
- Do not bake a maintainer's personal gateway URL or token into public firmware
  defaults.

## Versioning Policy

Tag the firmware fork after an upstream sync has been resolved and validated.
Use a tag name that makes the upstream base and StackChan validation point clear,
for example:

```bash
git tag stackchan-fw-2026q2
git push mine stackchan-fw-2026q2
```

The `stackchan-mcp` subtree pull should reference that validated fork tag. If a
future firmware version constant is added, update it in the same pull request as
the subtree import and mention it in the PR summary.

Do not rewrite public firmware fork history unless maintainers have explicitly
agreed to do so. Prefer merge commits for routine upstream syncs because they
preserve the exact conflict-resolution history.

## Fork Sync Playbook

Run these commands in the standalone firmware fork checkout, not in
`stackchan-mcp`:

```bash
cd /path/to/xiaozhi-esp32
git status
git remote -v
git fetch origin
git fetch mine
git switch -c stackchan-fw-sync-2026q2 mine/main
git merge --no-ff origin/main
```

Resolve conflicts in the fork. Pay special attention to:

- `main/boards/stackchan/`
- `main/Kconfig.projbuild`
- protocol and WebSocket changes
- display, touch, audio, and camera components used by the StackChan board
- release scripts and board metadata

After conflict resolution, build the StackChan board from the fork:

```bash
docker run --rm --cpus=4 --ulimit nofile=65536:65536 \
  -v "$PWD":/project -w /project espressif/idf:v5.5.2 \
  python ./scripts/release.py stackchan
```

If the build passes, run a quick hardware smoke test when hardware is available:

- flash only `build/xiaozhi.bin` at `0x20000` for normal update testing
- confirm the device boots
- confirm the gateway WebSocket reconnects
- check a small MCP surface such as `get_device_info`, `move_head`, and
  `take_photo`

Then push the fork sync branch:

```bash
git push -u mine stackchan-fw-sync-2026q2
```

Open a pull request in `kisaragi-mochi/xiaozhi-esp32` and merge it after review.
After the fork PR is merged, update local `main`, create the validation tag, and
push it:

```bash
git switch main
git pull --ff-only mine main
git tag stackchan-fw-2026q2
git push mine stackchan-fw-2026q2
```

## Monorepo Subtree Pull Playbook

Run these commands in this repository:

```bash
cd /path/to/stackchan-mcp
git status
git switch main
git pull --ff-only
git switch -c issue-10-firmware-sync
git remote add xiaozhi-fork https://github.com/kisaragi-mochi/xiaozhi-esp32.git
git fetch xiaozhi-fork --tags
git subtree pull --prefix=firmware xiaozhi-fork stackchan-fw-2026q2 --squash
```

The `git remote add` command changes only local git configuration; it is not
part of the commit. If `xiaozhi-fork` already exists, skip `git remote add` and
run:

```bash
git fetch xiaozhi-fork --tags
```

Review the subtree diff before committing:

```bash
git status
git diff --stat main...HEAD
git diff -- firmware/main/boards/stackchan/
```

Run the board-aware firmware build from the monorepo:

```bash
cd firmware
docker run --rm --cpus=4 --ulimit nofile=65536:65536 \
  -v "$PWD":/project -w /project espressif/idf:v5.5.2 \
  python ./scripts/release.py stackchan
```

The `--cpus=4` flag caps Docker container parallelism to keep the
LVGL / `xiaozhi-fonts/emoji_*.c` compile steps within the memory budget
on macOS Docker hosts (OrbStack / Docker Desktop); without it the build
can fail mid-LVGL with `Cannot allocate memory` even on hosts with
ample physical RAM (tracked as #112). The `--ulimit nofile=65536:65536`
flag separately avoids a `Too many open files` failure during the same
LVGL emoji compile step on macOS Docker defaults. See `README.md`
Option B for the full context.

For firmware changes, hardware verification is expected before merge when a
maintainer has the device available. If hardware is not available, open the PR
with the build result and clearly mark hardware verification as pending.

## Pull Request Checklist

Use one PR for the subtree update. In the PR body, include:

- the fork tag pulled into `firmware/`
- the upstream range or upstream commit used by the fork
- conflict areas and how they were resolved
- firmware build command and result
- hardware smoke test result, or why it is pending
- confirmation that no local secrets or personal gateway settings are included

Link the tracking issue with `Refs #N` or `Closes #N`.
