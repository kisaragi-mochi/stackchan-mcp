# Bundled Native Libraries

This directory contains pre-built native shared libraries that the
gateway needs on platforms where the system package manager does not
typically ship them. They are loaded at import time by
`stackchan_mcp/__init__.py` via `os.add_dll_directory()` (Windows) so
that `ctypes.util.find_library()` calls inside Python wrapper packages
(e.g. `opuslib`) resolve to the bundled copy without any user setup.

## Why bundle?

The Python wrapper packages that depend on these libraries (currently
`opuslib`, pulled in via the `[tts]` and `[stt]` extras) only ship
Python bindings — they do **not** ship the underlying native library.
On Linux and macOS most users already have `libopus` available through
their distro's package manager (`apt install libopus0`,
`brew install opus`, etc.), but on Windows there is no equivalent
default install path, which means a plain `pip install stackchan-mcp[tts]`
fails at runtime with `Could not find Opus library. Make sure it is
installed.` even though the Python wrappers installed cleanly.

Bundling the Windows binary in the wheel removes that footgun: every
Windows user who installs `stackchan-mcp[tts]` (or `[stt]`) gets a
working installation on the first try, with no extra `vcpkg` /
`conda install -c conda-forge libopus` / manual DLL placement step.

The decision to bundle (vs. download at install time vs. require source
build) was made on these criteria:

| Criterion | Verdict for libopus |
|---|---|
| Maturity of the dependency | Mature (Opus is a frozen IETF codec, RFC 6716, 2012) |
| Frequency of security advisories | Very low (the codec parser is small and well-audited) |
| File size | ~480 KB — fits comfortably in the wheel |
| Re-distribution license | BSD 3-clause (Xiph) — redistribution allowed with attribution |
| Long-term availability of upstream | Excellent (Xiph.Org maintains the source indefinitely) |

If any of those change (e.g. a future ML-based bundle that ships
hundreds of MB), revisit and consider the "CI downloads a pinned
version at build time" approach instead.

## opus.dll

| Field | Value |
|---|---|
| Architecture | x86_64 (`win_amd64`) |
| File size | 491,520 bytes |
| SHA256 | `5fa25b62f72f880acf420120d8ca32d31997620cadaab26ae798d13355012d1b` |
| License | BSD 3-clause + Xiph extension — see <https://opus-codec.org/license/> |
| Provenance | Extracted from PyOgg 0.6.14a1 Windows wheel on PyPI |
| Source wheel SHA256 | `40f79b288b3a667309890885f4cf53371163b7dae17eb17567fb24ab467eca26` |
| Source wheel URL | <https://pypi.org/project/pyogg/0.6.14a1/#files> (file: `PyOgg-0.6.14a1-py2.py3-none-win_amd64.whl`) |

### Provenance note

This binary is currently sourced from the PyOgg project, which ships
a Windows build of `libopus` inside its wheel. PyOgg is itself a thin
ctypes wrapper around `libopus`, so the binary inside its wheel is a
functionally standard build of the upstream Xiph Opus source.

Before this change is finalized for merge into the upstream
`stackchan-mcp` repository, the long-term provenance plan is to switch
to a CI-built binary so the build trail is fully self-contained
inside `stackchan-mcp`'s own CI. The proposed pipeline:

1. Add a `windows-latest` job to `.github/workflows/build.yml` that
   installs `libopus` via `vcpkg install opus --triplet=x64-windows`.
2. Copy the resulting `opus.dll` from the vcpkg installed tree into
   `stackchan_mcp/_libs/`.
3. Verify SHA256 against a pinned expected value and fail the build
   on drift (so a vcpkg-side change can't silently swap the binary).
4. The wheel built by that job uploads `stackchan-mcp-*-win_amd64.whl`
   to PyPI.

The PyOgg-sourced binary in this PR is functionally equivalent and lets
us validate the bundling + `os.add_dll_directory()` machinery before
the CI build pipeline is wired up.

## License compliance

The Opus codec is distributed under the 3-clause BSD license (with the
optional Xiph patent grant), which permits redistribution in source or
binary form provided the copyright notice and license text are
preserved. The full license text is reproduced below.

```
Copyright 2001-2023 Xiph.Org, Skype Limited, Octasic,
                    Jean-Marc Valin, Timothy B. Terriberry,
                    CSIRO, Gregory Maxwell, Mark Borgerding,
                    Erik de Castro Lopo

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions
are met:

- Redistributions of source code must retain the above copyright
  notice, this list of conditions and the following disclaimer.

- Redistributions in binary form must reproduce the above copyright
  notice, this list of conditions and the following disclaimer in the
  documentation and/or other materials provided with the distribution.

- Neither the name of Internet Society, IETF or IETF Trust, nor the
  names of specific contributors, may be used to endorse or promote
  products derived from this software without specific prior written
  permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```
