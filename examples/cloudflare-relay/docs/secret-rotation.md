# Rotating the Shared Bearer Secret

The relay uses a single shared Bearer token (`SHARED_SECRET`)
end-to-end. The Stack-chan device sends it in the `Authorization`
header on every connection, the Worker verifies it, and the gateway
re-verifies the forwarded header (when started with
`STACKCHAN_TOKEN=<SHARED_SECRET>`). The secret should be rotated if
it is suspected to be leaked, or periodically as part of routine
credential hygiene.

Rotating it requires updating the value in three places: the
Worker's `SHARED_SECRET` secret, the device's `websocket.token` NVS
field, and the gateway's `STACKCHAN_TOKEN` environment variable. The
order below minimizes the window where any one of them is out of
sync.

## Procedure

### 1. Generate a new secret

    openssl rand -hex 32

Keep the output handy — you will set it on the Worker, the device,
and the gateway in the next steps.

### 2. Update the Worker

From `examples/cloudflare-relay/`:

    npx wrangler secret put SHARED_SECRET
    # paste the new secret when prompted

The Worker now expects the new value. The device still presents the
old one, so off-LAN connections will start receiving 401s from the
Worker until Step 3 lands.

### 3. Update the Stack-chan device

Boot the device into WiFi configuration mode and update
`websocket.token` in the web UI to the new secret. Save and reboot.

The device now presents the new value. The gateway still expects the
old one via `STACKCHAN_TOKEN`, so LAN-direct connections and the
gateway side of relayed connections will see 401 until Step 4 lands.

### 4. Update the gateway

Restart the gateway process with the new value in
`STACKCHAN_TOKEN`:

    STACKCHAN_TOKEN=<new value> uv run stackchan-mcp

The gateway now accepts the new token from both LAN-direct and
relayed connections.

### 5. Verify

Confirm that on-LAN (mDNS-direct) and off-LAN (Worker-relayed)
connections both succeed under the new secret. Check the Worker logs
(`npx wrangler tail`) for a successful Bearer verification on the
off-LAN path, and the gateway logs for a successful authentication on
the on-LAN path.

## Notes

- The rotation causes a brief window (between Step 2 and Step 4)
  where the device is out of sync with one or both of the Worker /
  gateway. Plan the rotation when a short disconnect is acceptable,
  or co-ordinate Step 3 and Step 4 closely.
- There is no automatic rotation mechanism in this example. For
  production use, consider implementing token rotation through a
  Workers KV / Durable Objects-backed scheme.
