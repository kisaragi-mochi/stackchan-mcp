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
- CHANGELOG entry: add an `[Unreleased]` line under the matching
  subsection (`### Firmware` for `firmware/` changes, `### Gateway` for
  `gateway/` changes, `### Docs` for cross-cutting docs that ship with a
  release). See "Changelog entries per PR" below for the enforcing CI
  gate and the list of paths that automatically skip the check.

Small, focused PRs are easier to review. Prefer one issue per PR, or one small
maintenance purpose per PR.

## CI

The build workflow in `.github/workflows/build.yml` runs on pull requests and
pushes to `main`. It currently verifies:

- Firmware: `python ./scripts/release.py stackchan` inside
  `espressif/idf:v5.5.2`
- Gateway: `uv sync --frozen`, `uv run ruff check .`, and `uv run pytest`
- CHANGELOG: PR-level enforcement that `CHANGELOG.md` is updated when
  `firmware/` or `gateway/` paths change (see "Changelog entries per PR"
  below)

CI is the shared baseline. For firmware changes, real hardware testing is still
needed before merge, but contributors without hardware are welcome to open PRs
and ask for maintainer verification.

## Changelog entries per PR

`CHANGELOG.md` is the source of truth for user-visible changes between
releases. Every PR that touches `firmware/` or `gateway/` must add an
entry under the matching `[Unreleased]` subsection:

- `[Unreleased] > ### Firmware` for `firmware/` changes
- `[Unreleased] > ### Gateway`  for `gateway/` changes
- `[Unreleased] > ### Docs`     for cross-cutting documentation that
  ships alongside a release

The `Build` workflow runs a `CHANGELOG check` job on every PR. When
the PR touches `firmware/` or `gateway/`, the gate verifies that the
`[Unreleased]` section of `CHANGELOG.md` changed between the PR base
and head. A `CHANGELOG.md` edit somewhere else in the file (typo
fix on an older release section, comparison-link adjustments, blank
lines) does **not** satisfy the gate — the change has to land
inside `[Unreleased]`.

The gate also runs a light structure check: there must be exactly
one `## [Unreleased]` heading at HEAD, and it must appear before the
first dated release heading. These are zero-cost invariants that
keep release-prep tooling (`firmware-release.yml` extraction,
promotion flow) working reliably.

### Scope of this gate (and what it leaves to humans)

The gate's mission is the **common miss case**: a source-touching
PR that forgets to add a CHANGELOG entry. It is intentionally
lenient about everything else — mixing a typo polish with a new
entry, consolidating duplicates, reclassifying a bullet between
`### Firmware` and `### Gateway`, large reorderings. Those are
legitimate maintainer workflows and CI should not block them.

Content judgement on whether the entries are accurate, appropriately
worded, or in the right subsection belongs to maintainer review of
the CHANGELOG diff at merge time — and is double-checked at
release-prep time, when the maintainer promotes `[Unreleased]`
entries into the new dated section. Over-strict CI here would only
slow contributors down without giving end users a tangible win.

**Automatic skip** (no CHANGELOG entry required):

- Machine-generated lockfile sync only (`gateway/uv.lock`,
  `gateway/poetry.lock`)

If your PR is purely a docs-only / housekeeping change that does not
touch `firmware/` or `gateway/` at all, the gate does not fire and no
CHANGELOG entry is needed.

### Why this gate exists

Without this gate, the prior workflow was that contributors would land
source changes without an `[Unreleased]` entry, and the gap was only
discovered during release prep — at which point a maintainer had to file
a back-fill PR for every missing entry before the release could go out
(this happened across firmware-v1.4.1, v1.5.0, v1.6.0, v1.7.0, and again
during the v1.8.0 prep). Catching the missed entry on each PR avoids
that back-fill cycle and keeps `CHANGELOG.md` accurate as commits land,
rather than being reconstructed retroactively.

This gate complements the `firmware-release.yml` release-time gate that
enforces promotion of `[Unreleased]` entries into a dated
`## [firmware-vX.Y.Z]` section at tag-push time. Both gates target the
same accuracy invariant from different ends of the lifecycle.

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
docker run --rm --cpus=4 --ulimit nofile=65536:65536 \
  -v "$PWD":/project -w /project espressif/idf:v5.5.2 \
  python ./scripts/release.py stackchan
```

This produces `build/merged-binary.bin` and
`releases/v2.2.6_stackchan.zip`.

The `--cpus=4` flag caps Docker container parallelism so concurrent
LVGL / `xiaozhi-fonts/emoji_*.c` compile steps stay within the memory
budget on macOS Docker hosts (OrbStack / Docker Desktop). Without it,
`ninja` autodetects job count from `/proc/cpuinfo` and the resulting
parallel `gcc` pressure can exhaust container memory mid-LVGL with
`Cannot allocate memory` even on hosts with ample physical RAM
(tracked as #112). The `--ulimit nofile=65536:65536` flag separately
prevents a `Too many open files` failure during the same LVGL emoji
compile step under macOS Docker (OrbStack / Docker Desktop) defaults.
Linux hosts with higher defaults are unaffected, but passing both flags
unconditionally is safe and matches the project's CI invocation.

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

## AGENTS.md and AGENTS.local.md

This repository provides tracked `AGENTS.md` files at multiple levels (root,
`gateway/`, `firmware/`, `firmware/main/boards/stackchan/`). These contain
review guidelines and public developer guides that automated tools (such as
Codex) and contributors can use.

If you need **personal local configuration** (custom paths, local tokens,
environment-specific notes), create an `AGENTS.local.md` file in the same
directory. It is gitignored and will not be committed.

**Migration note:** Before this change, `AGENTS.md` was gitignored, so you
may have created your own local `AGENTS.md` with personal configuration.
We apologize for the inconvenience — we needed to provide repository-level
review guidelines through tracked `AGENTS.md` files, and this unfortunately
changes the convention. To migrate, simply rename your existing file:

```bash
mv AGENTS.md AGENTS.local.md
# repeat for any subdirectory where you had one
```

After renaming, `git pull` will bring in the tracked `AGENTS.md` without
conflict, and your personal settings continue to work as `AGENTS.local.md`.
We chose the `.local.md` suffix specifically so this transition is a simple
rename with no loss of your configuration.

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

### Review priorities

This is a single-user hobby product on a home LAN, not a multi-tenant SaaS.
Reviews (both human and automated) should reflect that context:

- **User-first**: does the feature work for the person sitting next to the
  robot? That matters more than theoretical edge cases.
- **Main path correctness over edge-case hardening**: a broken happy path is
  P0; a rare race condition is P2 at most.
- **DoS / resource-exhaustion stays low priority**: one user, one device, one
  LAN — denial-of-service is not a meaningful threat model here.
- **Be kind**: frame comments as suggestions, explain *why*, and mark
  stylistic preferences as non-blocking.

These priorities are also encoded in the `## Review guidelines` section of
`AGENTS.md` so automated review tools follow the same approach.

## Documentation Language

The top-level user guide is maintained in both English and Japanese:

- `README.md`: English entry point for international contributors
- `README.ja.md`: Japanese entry point for the StackChan community

Developer-facing subdocuments such as `gateway/README.md`, `docs/*.md`, issue
templates, and pull request templates should use English as the baseline. Add a
Japanese companion file only when a document has clear end-user value for the
Japanese community and can be kept reasonably in sync.

Keep code comments, public issue descriptions, and pull request descriptions in
English unless Japanese is needed for a hardware name, quoted source, or
community-specific term.

## Releasing the gateway to PyPI

Maintainers publish the gateway to PyPI by tagging a release on `main`.
The `.github/workflows/publish.yml` workflow runs on every tag matching
`v*`, builds an sdist and a wheel from `gateway/`, and uploads them to
PyPI via Trusted Publishing.

### Release gates

The publish workflow only succeeds if all of the following hold:

- The tag commit is an ancestor of `origin/main` (no publishing from
  feature branches or arbitrary commits).
- The tag has a `v` prefix and matches the `project.version` field of
  `gateway/pyproject.toml` after PEP 440 normalization, so `v0.1.0-rc.1`
  matches a `pyproject.toml` version of `0.1.0rc1`, etc.
- The version is not a PEP 440 local version (e.g. `1.0+local`).
- `uv run ruff check .` and `uv run pytest` succeed inside `gateway/`.
- The build produces a `dist/` containing both an sdist and a wheel.

Pre-release tags (`v0.2.0a1`, `v1.0.0rc1`, etc.) are allowed.

### One-time setup

Already configured for the current maintainer; documented here so future
maintainers can rebuild the chain:

1. Register `stackchan-mcp` on PyPI as a Trusted Publisher pointing at
   this repository, the `publish.yml` workflow file name, and the `pypi`
   environment.
2. Create a GitHub Environment named `pypi` on this repository
   (Settings → Environments). Trusted Publishing uses short-lived OIDC
   tokens, so no API secret needs to be stored. Adding required
   reviewers on the `pypi` environment provides a manual gate before
   each publish.
3. Mark `v*` tags as protected (Settings → Tags → New protection rule)
   so only maintainers can create or move release tags.

### Per-release steps

1. On a topic branch, bump the version in `gateway/pyproject.toml`
   (`project.version`) and update `CHANGELOG.md`: rename the
   `## [Unreleased]` section to `## [X.Y.Z] - YYYY-MM-DD`, add a fresh
   empty `## [Unreleased]` above it, and update the comparison links at
   the bottom of the file (`[Unreleased]` should compare the new tag to
   `HEAD`, and a new `[X.Y.Z]` link should point at the release tag).
   Open a PR with these changes.
2. After the PR is merged, tag the resulting commit on `main` with a
   matching `vX.Y.Z` tag and push the tag:
   ```bash
   git switch main
   git pull
   git tag v0.1.0
   git push origin v0.1.0
   ```
3. The publish workflow validates the gates above, builds, and publishes
   to PyPI. If any gate fails the workflow stops before the upload step.
4. Confirm the new version on https://pypi.org/p/stackchan-mcp and try a
   fresh `pipx install stackchan-mcp` (or `uv tool install stackchan-mcp`,
   or `pip install stackchan-mcp` inside a virtualenv) in a clean
   environment.

PyPI does not allow re-uploading the same version. If a release goes out
and you need to retract it, mark it yanked on PyPI rather than deleting
it, and ship the fix under the next version.

### Pinning policy for the publish workflow

`pypa/gh-action-pypi-publish` is pinned to a specific `minor.patch` tag
because it has direct upload access to PyPI. Supporting actions are
pinned to the most stable form their upstream maintains:

- `actions/checkout`, `actions/upload-artifact`, `actions/download-artifact`:
  major-version tag (e.g. `@v4`). Upstream maintains `v4`, `v5`, ... as
  moving aliases.
- `astral-sh/setup-uv`: full `vX.Y.Z` tag. Starting with v8 the upstream
  ships immutable releases only and does not maintain a moving major-
  version alias, so `@v8` does not exist. Bump to a new full version in
  its own PR.

Bumping the publish action's pin should be done in its own PR.

The publish workflow also supports `workflow_dispatch` so that
maintainers can verify the build pipeline (lint / test / build)
without cutting a tag. The publish job is gated on `push` events, so
manual runs cannot release.

### Fork-friendly publishing

The Trusted Publisher and `pypi` GitHub Environment described above are
reserved for the canonical `stackchan-mcp` project on PyPI, owned by
`kisaragi-mochi/stackchan-mcp`. A fork that wants to publish its own
builds should not reuse the upstream PyPI project name or assume the
upstream `pypi` environment is reachable from the fork. PRs against
this repository should leave the existing `publish.yml` and `pypi`
environment configuration alone.

Forks that want to ship under a different name (for example,
`yourhandle-stackchan-mcp` on PyPI) have two practical paths:

1. **Trusted Publishing on the fork.** Pick a different PyPI project
   name, change `project.name` in `gateway/pyproject.toml` (and review
   `project.urls` and the `[project.scripts]` console-script name if
   the fork wants its own CLI command, plus the user-facing references
   in `gateway/README.md`) to match, register the fork's
   `<owner>/<repo>` and `publish.yml` against the new PyPI project
   name as a Trusted Publisher, and recreate a `pypi` GitHub
   Environment in the fork. Tag/version gates in `publish.yml` keep
   working as-is because they only check the local repo and
   `pyproject.toml`. This keeps the OIDC-based, no-API-token flow.
2. **PyPI API token in the fork.** If Trusted Publishing is not an
   option (publishing from a non-GitHub CI, an internal index, or
   ad-hoc local releases), generate a project-scoped PyPI API token
   for the fork's PyPI project and store it as a secret in the fork.
   Replace the OIDC publish step in the fork's `publish.yml` (or the
   equivalent step in the alternate CI) with a token-based upload such
   as `twine upload --username __token__ --password $PYPI_API_TOKEN
   dist/*`. Keep the publish job in a protected environment so the
   secret is only exposed to the publish step, and prefer a
   project-scoped token over a user-scoped one.

In either case, do not push fork-only credentials, fork-only project
names, or token-based upload steps back to upstream. The upstream
pipeline is intentionally strict about the canonical project name and
the OIDC-only path.

## Releasing the firmware

Maintainers publish firmware binaries (`merged-binary.bin`, `xiaozhi.bin`,
`v*_stackchan.zip`) to GitHub Releases by tagging a release on `main`. The
`.github/workflows/firmware-release.yml` workflow runs on every tag matching
`firmware-v*`, builds the firmware via the ESP-IDF Docker image, and attaches
the build artifacts to a GitHub Release.

Firmware tags (`firmware-vX.Y.Z`) advance independently of the PyPI
gateway tags (`vX.Y.Z`). Both can coexist on `main`; a single PR that
touches both sides in a coupled way should cut both tags and mention
the pairing in each release's notes.

### Release gates

The firmware-release workflow only succeeds if all of the following hold:

- The tag has a `firmware-v` prefix.
- `CHANGELOG.md` contains a matching dated section
  `## [firmware-vX.Y.Z] - YYYY-MM-DD` for the new tag. Without this
  section the workflow fails fast in the `Verify CHANGELOG.md ...`
  step before any build runs, so promoting `[Unreleased]` Firmware
  entries into a dated CHANGELOG section is **enforced**, not just
  documented. (This gate was added after firmware-v1.4.1 / v1.5.0 /
  v1.6.0 / v1.7.0 each shipped binaries without ever promoting the
  matching CHANGELOG entries; firmware-v1.8.0 is the first release
  cut under the enforced gate.)
- The build job produces `firmware/build/merged-binary.bin`,
  `firmware/build/xiaozhi.bin`, and `firmware/releases/v*_stackchan.zip`.

### Per-release steps

1. On a topic branch, promote the `[Unreleased]` Firmware entries
   destined for the new release into a dated section in `CHANGELOG.md`:
   - Add a new `## [firmware-vX.Y.Z] - YYYY-MM-DD` section and move the
     applicable entries from `[Unreleased] > Firmware` into it. Place
     the new section in chronological position relative to the other
     dated sections (newer dates go higher in the file). Same idea for
     a `Docs` subsection if the release also includes documentation
     changes that are not specific to one side.
   - Leave a fresh empty `## [Unreleased]` at the top so future merges
     have a clear destination.
   - Update the comparison-links block at the bottom of `CHANGELOG.md`:
     ```
     [Unreleased]: https://github.com/kisaragi-mochi/stackchan-mcp/compare/firmware-vX.Y.Z...HEAD
     [firmware-vX.Y.Z]: https://github.com/kisaragi-mochi/stackchan-mcp/compare/firmware-v<PREV>...firmware-vX.Y.Z
     ```
     (the `[Unreleased]` link tracks whichever side just released, so it
     may compare against either a firmware-v* or a v* tag depending on
     which release went out last).
   Open a PR with these changes.
2. After the PR is merged, tag the resulting commit on `main`:
   ```bash
   git switch main
   git pull
   git tag firmware-v1.8.0
   git push origin firmware-v1.8.0
   ```
3. The firmware-release workflow validates the gates above, builds the
   firmware in the ESP-IDF Docker image, and attaches the binaries to a
   fresh GitHub Release named after the tag. The default release body
   is the standard flash-instructions template; maintainers can edit it
   after the workflow finishes to add a release-specific `Highlights` /
   `License` / `Migration notes` section. The natural source is the new
   CHANGELOG dated section, which can be extracted with:
   ```bash
   # Extract the new section from CHANGELOG, dropping the section
   # header and the trailing section-boundary line.
   sed -n '/^## \[firmware-vX\.Y\.Z\]/,/^## \[/p' CHANGELOG.md \
     | sed '1d;$d' > /tmp/firmware-release-notes.md
   gh release edit firmware-vX.Y.Z --repo kisaragi-mochi/stackchan-mcp \
     --notes-file /tmp/firmware-release-notes.md
   ```
4. Confirm the new release on
   <https://github.com/kisaragi-mochi/stackchan-mcp/releases>.

### Yank policy

If a firmware release ships and a critical issue surfaces immediately,
prefer cutting `firmware-vX.Y.(Z+1)` with a fix rather than deleting the
tag — GitHub Releases preserves the activity feed event even if the
release page is deleted, so a yank cannot retroactively retract the
event for watchers. The `firmware-v1.4.0` release (yanked within ~12
minutes on 2026-05-14, replaced by `firmware-v1.4.1`) is the historical
example.

## Development Philosophy

These three principles guide labeling, milestoning, scoping, and issue / PR
triage in this repository. When a decision feels ambiguous, check it against
the three in order.

### 1. LLM-User First

The primary consumer of MCP tools is an LLM agent, not a human CLI user.
Usability for AI is the top priority.

- MCP tool signatures, error messages, and descriptions should be at a
  granularity where an LLM can read them and form a correct call on the
  first try
- "A human can figure this out by reading carefully" is a deduction;
  "an LLM nails the arguments first try" is the target
- Example: `set_head_angles(yaw_deg, pitch_deg, duration_ms)` — the
  `description` should spell out range, unit, recommended values, and
  end-stop warnings (the human README is separate; the tool description
  is for the LLM)
- Issue bodies and PR descriptions should pass the same check: can a fresh
  LLM session load context from this and decide what to do?

### 2. Contributors Welcome

External contributors should be able to read the repo and participate
without insider context.

- Issues / PRs / README / CONTRIBUTING / code comments should be
  understandable to a cold-start contributor
- Labels and milestones should not look "untriaged" or "abandoned"
- Use `good first issue` / `help wanted` sparingly but meaningfully
- The issue surface should make it easy for a new contributor to find an
  issue they can pick up (Projects v2 view is planned for this)

### 3. Give Back to the StackChan Community

`stackchan-mcp` exists inside and contributes to the broader StackChan
ecosystem originated by Shinya Ishikawa (ししかわ).

- Attribution to upstream and related projects (`stack-chan/stack-chan`,
  `stack-chan/stackchan-arduino`, `m5stack/m5stack-avatar`,
  `78/xiaozhi-esp32`, `necobit/feetech_scs_esp_idf`, etc.) is carefully
  preserved; misattribution gets corrected promptly
- When a finding or fix is discovered upstream, return it as a PR or
  Issue to the relevant project (example: filing on
  `necobit/feetech_scs_esp_idf` on 2026-05-10)
- "StackChan" and "スタックチャン" are registered trademarks of Shinya
  Ishikawa — the `## Trademarks` section in `README.md` is required
- Feedback to community contributions stays polite, in the desu/masu
  register in Japanese contexts, with minimal emoji and a sincere
  "thank you / good catch / let me look into it" reflex

### When to apply

- **Filing a new issue**: explicitly state which of the three the issue
  improves. If none of the three are clearly served, don't file it
  (comment on an existing issue or add to docs instead)
- **Scoping a PR**: when scope creeps, draw the line on "which of the
  three does this PR primarily serve"
- **Priority decisions**: raising priority to `medium` or higher needs a
  middling-or-higher impact on at least one of the three
- **When uncertain**: ask the maintainer "which of the three do we
  prioritize for this one?"

## Communication

Be polite and concrete. This is a small hardware community, and clear technical
notes help the next person reproduce what happened.
