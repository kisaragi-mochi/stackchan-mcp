# Rotating the Shared Bearer Secrets

The relay uses two separate Bearer tokens — `SHARED_SECRET` for the
device-to-Worker boundary and `UPSTREAM_TOKEN` for the Worker-to-gateway
boundary — and each should be rotated if it is suspected to be leaked,
or periodically as part of routine credential hygiene. This document
describes the manual rotation procedure.

Rotate the two secrets independently. The procedure below applies to
each one separately; complete it for `SHARED_SECRET` first, then for
`UPSTREAM_TOKEN` (or only the one you need to rotate).

## Rotating `SHARED_SECRET` (device <-> Worker)

### 1. Generate a new secret

    openssl rand -hex 32

Keep the output handy — you will set it on the Worker and on the
device in the next steps.

### 2. Update the Worker

From `examples/cloudflare-relay/`:

    npx wrangler secret put SHARED_SECRET
    # paste the new secret when prompted

This overwrites the existing secret. The Worker will reject the
device on its next connection attempt because the device is still
presenting the old token.

### 3. Update the Stack-chan device

Boot the device into WiFi configuration mode and update
`websocket.token` in the web UI to the new secret. Save and reboot.

### 4. Verify

Take the device off-LAN and confirm it reconnects via the Worker.
Check the Worker logs (`npx wrangler tail`) for a successful Bearer
verification.

## Rotating `UPSTREAM_TOKEN` (Worker <-> gateway)

If you are running the gateway without `--token`, you do not have
this secret to rotate; you can skip this section.

### 1. Generate a new secret

    openssl rand -hex 32

### 2. Update the Worker

From `examples/cloudflare-relay/`:

    npx wrangler secret put UPSTREAM_TOKEN
    # paste the new secret when prompted

### 3. Update the gateway

Restart the gateway with the new `--token <new-value>`. There is
typically a brief window where the Worker presents the new token but
the old gateway process is still running; minimize this by restarting
the gateway immediately after Step 2.

### 4. Verify

Take the device off-LAN and confirm the relay path still works (the
device-side connection is unaffected by `UPSTREAM_TOKEN` rotation;
all the work happens between the Worker and the gateway).

## Notes

- Each rotation causes a brief window (between Worker secret update
  and the corresponding device or gateway update) where that side of
  the relay cannot reach the other. On-LAN traffic is unaffected
  because it uses mDNS directly between the device and the gateway.
- There is no automatic rotation mechanism in this example. For
  production use, consider implementing token rotation through a
  Workers KV / Durable Objects-backed scheme.
