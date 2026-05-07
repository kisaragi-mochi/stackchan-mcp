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

## Communication

Be polite and concrete. This is a small hardware community, and clear technical
notes help the next person reproduce what happened.
