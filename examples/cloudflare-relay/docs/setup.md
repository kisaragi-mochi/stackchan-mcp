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
        service: http://localhost:8765
      - service: http_status:404

Then route the chosen hostname to the tunnel:

    cloudflared tunnel route dns stackchan-relay-backend \
      stackchan-relay-backend.<your-domain>

## Step 3: Run cloudflared as a service

Install `cloudflared` as a launchd service so it survives reboots:

    sudo cloudflared service install

Verify it is running:

    cloudflared tunnel info stackchan-relay-backend

## Step 4: Generate the shared secret

This example uses a single shared Bearer token end-to-end. The
firmware sends it on every connection (on-LAN mDNS-direct and
off-LAN relayed); the Worker verifies it and forwards the same header
to the gateway; the gateway (when started with `STACKCHAN_TOKEN`)
re-verifies it. Using a single token keeps both the LAN and relay
paths working with the same NVS configuration on the device.

On your deployment workstation:

    openssl rand -hex 32

Save the output. You will set it on the Worker, on the Stack-chan
device, and on the gateway.

## Step 5: Deploy the Worker

From `examples/cloudflare-relay/`:

    npx wrangler login

Edit `wrangler.toml` and set `UPSTREAM_URL` to the tunnel hostname
configured in Step 2, prefixed with `https://` (not `wss://` — the
Worker performs the WebSocket upgrade by issuing `fetch()` with
`Upgrade: websocket`, which requires an http/https URL). For example:

    [vars]
    UPSTREAM_URL = "https://stackchan-relay-backend.<your-domain>"

Register the shared secret (do not commit it):

    npx wrangler secret put SHARED_SECRET
    # paste the secret from Step 4 when prompted

Deploy:

    npx wrangler deploy

Wrangler will print the Worker URL (for example,
`https://stackchan-relay.<your-subdomain>.workers.dev`). Convert this
to a `wss://` URL — that becomes the value you set on the Stack-chan
device in Step 7.

## Step 6: Configure the gateway

Restart the gateway with the shared secret in the `STACKCHAN_TOKEN`
environment variable. The gateway authenticates incoming WebSocket
connections (both LAN-direct from the device and relayed from the
Worker) against this value:

    STACKCHAN_TOKEN=<shared-secret value> uv run stackchan-mcp

If you skip setting `STACKCHAN_TOKEN`, the gateway accepts connections
without authentication. The tunnel hostname then becomes an
unauthenticated endpoint — anyone who learns the hostname can reach
the gateway without going through the Worker.

## Step 7: Configure the Stack-chan device

Boot the device into its WiFi configuration access point (refer to the
main `stackchan-mcp` README for how to enter config mode). In the web
UI, set:

- `websocket.url` → leave empty (the firmware uses mDNS auto-discovery
  for the LAN case).
- `websocket.fallback_url` → the Worker URL from Step 5, as `wss://`.
- `websocket.token` → the shared secret from Step 4.

Save and reboot the device. The firmware will:

1. Attempt mDNS discovery first (LAN case, about 5s timeout).
2. If mDNS yields no candidates, fall back to the Worker URL.
3. Send the Bearer token in the `Authorization` header on every
   candidate (so the gateway authenticates both LAN-direct and
   relayed connections with the same value).

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
- `relay misconfigured: SHARED_SECRET unset` (HTTP 500) from the
  Worker: the `SHARED_SECRET` Wrangler secret is not registered.
  Run `npx wrangler secret put SHARED_SECRET` (see Step 5).
- `upstream unreachable` (HTTP 502) from the Worker: usually one of:
  - `cloudflared` is not connected on the gateway host (check
    `cloudflared tunnel info` there).
  - The tunnel ingress is misconfigured.
  - The gateway is not running on `localhost:8765`.
  - The gateway is started with `STACKCHAN_TOKEN=<value>` but the
    Worker has a different `SHARED_SECRET`. The gateway then
    returns 401 and the Worker surfaces 502. Make sure the two
    values are identical.
- The device never falls back to the Worker URL on-LAN: this is
  expected. mDNS auto-discovery wins on-LAN; the Worker URL is only
  used when mDNS fails to resolve a candidate.
