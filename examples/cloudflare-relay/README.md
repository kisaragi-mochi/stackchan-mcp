# Cloudflare Workers Relay (optional example)

This directory contains a self-contained example for relaying WebSocket
connections from a Stack-chan device to a self-hosted `stackchan-mcp`
gateway through Cloudflare Workers, enabling the device to reach the
gateway from outside the local LAN.

This example is provided as-is and is not part of the maintained
`stackchan-mcp` dependency surface. It is intended as a reference for
operators who want to make their gateway reachable over the public
internet via Cloudflare, while keeping the gateway machine itself
behind NAT.

## Overview

    Stack-chan (ESP32 firmware)
      └── WSS (Authorization: Bearer <SHARED_SECRET>)
            │
            ▼
      Cloudflare Workers (this example)
       ─ verifies Bearer token (constant-time compare)
       ─ proxies the WebSocket bidirectionally
       ─ forwards the same Authorization header upstream
            │
            ▼ fetch (WS upgrade, https://)
      Cloudflare Tunnel hostname (cloudflared on the gateway host)
            │
            ▼
      stackchan-mcp gateway (ws://localhost:8765,
        started with STACKCHAN_TOKEN=<SHARED_SECRET> so it
        authenticates both mDNS-direct and relayed connections
        with the same Bearer)

The Stack-chan firmware tries mDNS auto-discovery first (LAN case), and
falls back to the configured `websocket.fallback_url` (this Worker's
URL) when mDNS does not resolve. No firmware changes are required.

## When to use this

Use this example if all of the following apply:

- You want a Stack-chan device to reach the gateway when it is not on
  the same LAN as the gateway machine.
- The gateway machine is reachable 24/7 and can run `cloudflared` as a
  background service.
- You are comfortable maintaining a personal Cloudflare account and a
  Workers Paid plan (around 5 USD per month).

If you only operate Stack-chan on a single LAN, this example is not
needed — the default mDNS auto-discovery flow handles that case
without any external dependency.

## Components

| File                             | Purpose                                          |
| -------------------------------- | ------------------------------------------------ |
| `src/index.ts`                   | The Worker: Bearer auth + bidirectional WS proxy |
| `wrangler.toml`                  | Cloudflare Workers deployment configuration      |
| `package.json` / `tsconfig.json` | TypeScript build setup                           |
| `docs/setup.md`                  | Step-by-step setup (Tunnel, deploy, NVS config)  |
| `docs/secret-rotation.md`        | How to rotate the shared Bearer secret           |

## Trade-offs

- First outbound connection takes about 15 seconds because the
  firmware first attempts mDNS (5s timeout) and then falls back to the
  Cloudflare endpoint (10s server-hello window). Subsequent reconnects
  use exponential backoff.
- Traffic is proxied through Cloudflare edge, adding small latency on
  top of the direct Tunnel-to-gateway hop.
- This example uses a single shared Bearer token (`SHARED_SECRET`)
  end-to-end. The firmware sends it on every WebSocket connection
  (both mDNS-direct on-LAN and Worker-relayed off-LAN); the Worker
  verifies it and forwards the same `Authorization` header upstream;
  the gateway re-verifies it when started with
  `STACKCHAN_TOKEN=<SHARED_SECRET>`. A single shared secret matches
  the firmware's behaviour of sending the same NVS `websocket.token`
  on every candidate. Treat it as you would any production secret;
  see `docs/secret-rotation.md` for rotation guidance.
- This example deliberately avoids beta Cloudflare features (e.g.,
  Workers VPC bindings). It uses only generally-available primitives
  (Workers WS API, Cloudflare Tunnel public hostnames).

## License

MIT — same as the rest of this repository. See the top-level `LICENSE`
file.
