# Setup Guide

This document walks through deploying the Cloudflare Workers relay
example end-to-end: setting up a Cloudflare Tunnel on the gateway host,
deploying the Worker, and pointing the Stack-chan firmware at the
relay.

## Prerequisites

- A Cloudflare account.
- A domain registered with Cloudflare (so you can add DNS records).
  This example currently requires a custom domain because named
  tunnels do not auto-publish hostnames under `*.cfargotunnel.com`,
  and setting up a custom hostname via `cloudflared tunnel route dns`
  requires control of the DNS zone.
- Node.js (>= 18) and npm installed on your workstation.
- A machine that hosts the `stackchan-mcp` gateway and is reachable
  24/7 (this guide assumes macOS; Linux / Windows users should adapt
  the install steps for `cloudflared`).
- The `stackchan-mcp` gateway already running and listening on
  `ws://localhost:8765` on the gateway host.

Install the per-host tools:

    # On the gateway host (where stackchan-mcp gateway runs)
    brew install cloudflared

    # On your deployment workstation (can be the same machine)
    cd examples/cloudflare-relay
    npm install

## Step 1: Create the Cloudflare Tunnel

On the gateway host, log in to Cloudflare and create a tunnel:

    cloudflared tunnel login
    cloudflared tunnel create stackchan-relay-backend

The `create` command prints a UUID and writes a credentials file under
`~/.cloudflared/`. Note both — you will reference the UUID in the
ingress config below.

## Step 2: Configure Tunnel ingress

Create `~/.cloudflared/config.yml` with the following content,
substituting the tunnel UUID and your chosen hostname (must be under
a Cloudflare-managed domain you own):

    tunnel: <tunnel-uuid>
    credentials-file: /Users/<you>/.cloudflared/<tunnel-uuid>.json

    ingress:
      - hostname: stackchan-relay-backend.<your-domain>
        service: ws://localhost:8765
      - service: http_status:404

Then route the chosen hostname to the tunnel:

    cloudflared tunnel route dns stackchan-relay-backend \
      stackchan-relay-backend.<your-domain>

## Step 3: Run cloudflared as a service

Install `cloudflared` as a launchd service so it survives reboots:

    sudo cloudflared service install

Verify it is running:

    cloudflared tunnel info stackchan-relay-backend

## Step 4: Generate the shared secrets

This example uses two separate Bearer tokens — one for the
device-to-Worker boundary, one for the Worker-to-gateway boundary —
so each segment can be rotated independently.

On your deployment workstation:

    openssl rand -hex 32   # SHARED_SECRET (device <-> Worker)
    openssl rand -hex 32   # UPSTREAM_TOKEN (Worker <-> gateway)

Save both outputs. You will set them on the Worker, on the
Stack-chan device, and on the gateway in the next steps.

If you intend to run the gateway without authentication (i.e., not
passing `--token` to the gateway), you can skip generating
`UPSTREAM_TOKEN`. Be aware that the tunnel hostname then becomes an
unauthenticated endpoint — anyone who learns the hostname can reach
the gateway directly without going through the Worker.

## Step 5: Deploy the Worker

From `examples/cloudflare-relay/`:

    npx wrangler login

Edit `wrangler.toml` and set `UPSTREAM_URL` to the tunnel hostname
configured in Step 2, prefixed with `https://` (not `wss://` — the
Worker performs the WebSocket upgrade by issuing `fetch()` with
`Upgrade: websocket`, which requires an http/https URL). For example:

    [vars]
    UPSTREAM_URL = "https://stackchan-relay-backend.<your-domain>"

Register the device-to-Worker shared secret (do not commit it):

    npx wrangler secret put SHARED_SECRET
    # paste the SHARED_SECRET value from Step 4 when prompted

If you generated `UPSTREAM_TOKEN` in Step 4, register it too:

    npx wrangler secret put UPSTREAM_TOKEN
    # paste the UPSTREAM_TOKEN value from Step 4 when prompted

Deploy:

    npx wrangler deploy

Wrangler will print the Worker URL (for example,
`https://stackchan-relay.<your-subdomain>.workers.dev`). Convert this
to a `wss://` URL — that becomes the value you set on the Stack-chan
device in Step 7.

## Step 6: Configure the gateway

If you want the gateway to require an upstream Bearer token (matching
the `UPSTREAM_TOKEN` registered with the Worker), start the gateway
with `--token`. For example:

    uv run stackchan-mcp --token <UPSTREAM_TOKEN value>

If you skip this and start the gateway without `--token`, the tunnel
hostname becomes an unauthenticated endpoint (see Step 4 trade-off).

## Step 7: Configure the Stack-chan device

Boot the device into its WiFi configuration access point (refer to the
main `stackchan-mcp` README for how to enter config mode). In the web
UI, set:

- `websocket.url` → leave empty (the firmware uses mDNS auto-discovery
  for the LAN case).
- `websocket.fallback_url` → the Worker URL from Step 5, as `wss://`.
- `websocket.token` → the `SHARED_SECRET` value from Step 4.

Save and reboot the device. The firmware will:

1. Attempt mDNS discovery first (LAN case, about 5s timeout).
2. If mDNS yields no candidates, fall back to the Worker URL.
3. Send the Bearer token in the `Authorization` header for the Worker
   to verify.

## Step 8: Verify

With the device on the same LAN as the gateway, it should connect
directly via mDNS (no relay traffic). Confirm by checking the gateway
logs for an incoming WebSocket connection from the device's LAN IP.

Then take the device off-LAN (e.g., tether to a mobile hotspot). The
device should connect within roughly 15 seconds: ~5s mDNS timeout +
~10s server-hello window on the Worker candidate. Confirm by checking
the Worker logs (`npx wrangler tail`) for a connection accepted with
the Bearer token verified.

## Troubleshooting

- `unauthorized` (HTTP 401) from the Worker: the Bearer token on the
  device does not match the Worker's `SHARED_SECRET`. Re-check both,
  and rotate the secret if leaked (see `secret-rotation.md`).
- `upstream unreachable` (HTTP 502) from the Worker: usually one of:
  - `cloudflared` is not connected on the gateway host (check
    `cloudflared tunnel info` there).
  - The tunnel ingress is misconfigured.
  - The gateway is not running on `localhost:8765`.
  - The gateway is started with `--token <upstream-secret>` but the
    Worker does not have `UPSTREAM_TOKEN` set (the gateway returns
    401 and the Worker surfaces 502). Register `UPSTREAM_TOKEN` via
    `wrangler secret put` (see Step 5) so it matches the gateway's
    `--token` value.
- The device never falls back to the Worker URL on-LAN: this is
  expected. mDNS auto-discovery wins on-LAN; the Worker URL is only
  used when mDNS fails to resolve a candidate.
