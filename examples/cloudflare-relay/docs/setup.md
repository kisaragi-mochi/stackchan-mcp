# Setup Guide

This document walks through deploying the Cloudflare Workers relay
example end-to-end: setting up a Cloudflare Tunnel on the gateway host,
deploying the Worker, and pointing the Stack-chan firmware at the
relay.

## Prerequisites

- A Cloudflare account.
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
substituting the tunnel UUID and your chosen hostname:

    tunnel: <tunnel-uuid>
    credentials-file: /Users/<you>/.cloudflared/<tunnel-uuid>.json

    ingress:
      - hostname: stackchan-relay-backend.<your-domain>
        service: ws://localhost:8765
      - service: http_status:404

If you do not own a domain on Cloudflare, you can use the
`<tunnel-name>.cfargotunnel.com` hostname that the tunnel exposes by
default; replace the `hostname:` value accordingly.

Then route the chosen hostname to the tunnel:

    cloudflared tunnel route dns stackchan-relay-backend \
      stackchan-relay-backend.<your-domain>

## Step 3: Run cloudflared as a service

Install `cloudflared` as a launchd service so it survives reboots:

    sudo cloudflared service install

Verify it is running:

    cloudflared tunnel info stackchan-relay-backend

## Step 4: Generate a shared secret

On your deployment workstation:

    openssl rand -hex 32

Save the output — you will set it on the Worker and on the Stack-chan
device.

## Step 5: Deploy the Worker

From `examples/cloudflare-relay/`:

    npx wrangler login

Edit `wrangler.toml` and set `UPSTREAM_URL` to the tunnel hostname
configured in Step 2, prefixed with `wss://`. For example:

    [vars]
    UPSTREAM_URL = "wss://stackchan-relay-backend.<your-domain>"

Register the shared secret (do not commit it):

    npx wrangler secret put SHARED_SECRET
    # paste the secret from Step 4 when prompted

Deploy:

    npx wrangler deploy

Wrangler will print the Worker URL (for example,
`https://stackchan-relay.<your-subdomain>.workers.dev`). Convert this
to a `wss://` URL — that becomes the value you set on the Stack-chan
device in Step 6.

## Step 6: Configure the Stack-chan device

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
3. Send the Bearer token in the `Authorization` header for the Worker
   to verify.

## Step 7: Verify

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
- `upstream unreachable` (HTTP 502) from the Worker: `cloudflared` is
  not connected, the tunnel ingress is misconfigured, or the gateway
  is not running on `localhost:8765`. Check `cloudflared tunnel info`
  on the gateway host.
- The device never falls back to the Worker URL on-LAN: this is
  expected. mDNS auto-discovery wins on-LAN; the Worker URL is only
  used when mDNS fails to resolve a candidate.
