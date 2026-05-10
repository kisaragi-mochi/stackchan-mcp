# feetech_scs (vendored)

MIT-licensed Feetech SCS0009 / SCSCL serial servo driver for ESP-IDF, vendored
from upstream as a clean-room alternative to the GPL-3.0 `SCServo_lib` shipped
in `firmware/main/boards/stackchan/`.

## Source

- Upstream: <https://github.com/necobit/feetech_scs_esp_idf>
- Commit:   `38a91984be0d30abbff30d2f56dd3072f2905faf` (initial provisional
            release, 2026-05-10)
- License:  MIT — see `LICENSE` in this directory (verbatim copy of upstream
            `LICENSE`).

The upstream source header (`feetech_scs.cpp`) explicitly states the
implementation is a **clean-room reimplementation from the public Feetech
SCS0009 datasheet** with **no code derived from the GPLv3 SCServo_lib**.

## Local modifications

This vendor copy is **not byte-for-byte identical** to upstream. The
following local patches have been applied; each is planned to be sent
back to upstream as an Issue/PR once real-device validation is complete:

1. **`uart_wait_tx_done()` between TX and RX in `write_reg` / `read_reg`**
   ([#79](https://github.com/kisaragi-mochi/stackchan-mcp/issues/79)).
   Upstream starts reading the bus immediately after `uart_write_bytes`,
   which only enqueues the bytes into the UART driver buffer. On a
   half-duplex servo bus this lets our own outgoing bytes clip the ACK
   packet and produces spurious bus-error returns. The original
   `SCServo_lib` mirrors a TX-drain wait via
   `SCSerial::wFlushSCS` (`uart_wait_tx_done(..., 100ms)`); we
   reproduce that here. Tagged in the source with
   `// Local patch (issue #79):` markers for easy diffing against
   upstream.

The `LICENSE` file remains verbatim from upstream — MIT license
attribution is preserved.

## Why vendored

The upstream project is currently in provisional release state. Vendoring a
pinned commit lets us:

1. Audit the exact bytes that ship in firmware builds.
2. Iterate on integration locally (Kconfig, build glue) without waiting for
   upstream changes.
3. Feed verification findings back upstream as Issues / PRs.

Once upstream stabilizes, this can be migrated to a `submodule` or
`idf_component.yml` managed dependency.

## Updating

To refresh this vendor copy from upstream:

```bash
cd firmware/components/feetech_scs
curl -L https://raw.githubusercontent.com/necobit/feetech_scs_esp_idf/<sha>/feetech_scs.h -o feetech_scs.h
curl -L https://raw.githubusercontent.com/necobit/feetech_scs_esp_idf/<sha>/feetech_scs.cpp -o feetech_scs.cpp
curl -L https://raw.githubusercontent.com/necobit/feetech_scs_esp_idf/<sha>/CMakeLists.txt -o CMakeLists.txt
curl -L https://raw.githubusercontent.com/necobit/feetech_scs_esp_idf/<sha>/LICENSE -o LICENSE
```

Update the commit SHA in this README's "Source" section to match.

## Tracking issue

See [`#79`](https://github.com/kisaragi-mochi/stackchan-mcp/issues/79) for the
evaluation status, validation plan, and migration roadmap to MIT-only firmware.
