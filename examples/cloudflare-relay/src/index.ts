/// <reference types="@cloudflare/workers-types" />

interface Env {
  SHARED_SECRET: string;
  UPSTREAM_URL: string;
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    // 1. Verify this is a WebSocket upgrade request.
    const upgrade = req.headers.get("Upgrade");
    if (upgrade !== "websocket") {
      return new Response("expected websocket", { status: 426 });
    }

    // 2. Verify the Authorization Bearer token using constant-time comparison.
    //    Fail closed if SHARED_SECRET is unset — without this guard, a
    //    misconfigured deployment would compare against the literal
    //    string "Bearer undefined" and authenticate any client that
    //    presented that value.
    if (!env.SHARED_SECRET) {
      return new Response(
        "relay misconfigured: SHARED_SECRET unset",
        { status: 500 },
      );
    }
    const auth = req.headers.get("Authorization") ?? "";
    const expected = "Bearer " + env.SHARED_SECRET;
    if (!constantTimeEqual(auth, expected)) {
      return new Response("unauthorized", { status: 401 });
    }

    // 3. Open the upstream WebSocket through the configured Cloudflare Tunnel.
    //    Forward the device's Authorization header to the gateway so
    //    a gateway started with STACKCHAN_TOKEN=<SHARED_SECRET> can
    //    re-verify the same Bearer. The firmware sends the same NVS
    //    `websocket.token` on every candidate, so on-LAN mDNS-direct
    //    and off-LAN relayed connections present the same value to
    //    the gateway.
    const upstreamReq = new Request(env.UPSTREAM_URL, {
      headers: {
        Upgrade: "websocket",
        Authorization: auth,
        "Protocol-Version": req.headers.get("Protocol-Version") ?? "",
        "Device-Id": req.headers.get("Device-Id") ?? "",
        "Client-Id": req.headers.get("Client-Id") ?? "",
      },
    });
    const upstreamRes = await fetch(upstreamReq);
    if (upstreamRes.status !== 101 || !upstreamRes.webSocket) {
      return new Response("upstream unreachable", { status: 502 });
    }
    const upstreamWS = upstreamRes.webSocket;
    upstreamWS.accept();

    // 4. Create a WebSocketPair to return to the client.
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    server.accept();

    // 5. Pipe messages bidirectionally. Text and binary frames are both
    //    delivered through event.data without further inspection.
    server.addEventListener("message", (e) => upstreamWS.send(e.data));
    upstreamWS.addEventListener("message", (e) => server.send(e.data));

    // 6. Propagate close and error events in both directions so neither
    //    side is left holding a half-open connection.
    server.addEventListener("close", () => upstreamWS.close());
    upstreamWS.addEventListener("close", () => server.close());
    server.addEventListener("error", () => upstreamWS.close());
    upstreamWS.addEventListener("error", () => server.close());

    // 7. Return the client side of the WebSocketPair as the response.
    return new Response(null, { status: 101, webSocket: client });
  },
};

// Constant-time string comparison to mitigate timing attacks on the
// Bearer secret. The length-mismatch fast path is not secret-derived
// in this code path (the expected length is fixed by SHARED_SECRET).
function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let result = 0;
  for (let i = 0; i < a.length; i++) {
    result |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return result === 0;
}
