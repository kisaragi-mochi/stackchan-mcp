# Rotating the Shared Bearer Secret

The Bearer token shared between the Worker and the Stack-chan device
should be rotated if it is suspected to be leaked, or periodically as
part of routine credential hygiene. This document describes the
manual rotation procedure.

## Procedure

### 1. Generate a new secret

On your deployment workstation:

    openssl rand -hex 32

Keep the output handy — you will set it in both the Worker and the
device's NVS in the next steps.

### 2. Update the Worker

From `examples/cloudflare-relay/`:

    npx wrangler secret put SHARED_SECRET
    # paste the new secret when prompted

This overwrites the existing secret. The Worker will reject the device
on its next connection attempt because the device is still presenting
the old token.

### 3. Update the Stack-chan device

Boot the device into WiFi configuration mode and update
`websocket.token` in the web UI to the new secret. Save and reboot.

### 4. Verify reconnection

Take the device off-LAN and confirm it reconnects via the Worker.
Check the Worker logs (`npx wrangler tail`) for a successful Bearer
verification.

If the device cannot reconnect, double-check that the device's
`websocket.token` matches the value you pasted into
`wrangler secret put`.

## Notes

- This procedure causes a brief window (between Step 2 and Step 3)
  where the device cannot reach the Worker. On-LAN traffic is
  unaffected because it uses mDNS directly.
- There is no automatic rotation mechanism in this example. For
  production use, consider implementing token rotation through a
  Workers KV / Durable Objects-backed scheme.
