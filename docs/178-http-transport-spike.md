# Issue #178 Phase A spike: HTTP transport choice

Date: 2026-06-01

## Summary

Issue #178 should use **Streamable HTTP** for the Phase B daemon transport,
not the legacy HTTP+SSE transport.

The decisive factors are:

- The current stable MCP transport specification, version `2025-06-18`,
  defines Streamable HTTP as the modern HTTP transport and describes legacy
  HTTP+SSE only as a backwards-compatibility path.
- The MCP Python SDK installed in this repository's gateway environment is
  `mcp==1.27.0`, and its FastMCP and low-level APIs both have first-class
  Streamable HTTP server support.
- Streamable HTTP's single MCP endpoint, request-scoped responses, session
  header support, and optional resumability map cleanly to the daemon plus
  command-queue architecture in #178.

Phase B should implement a single long-running gateway daemon that owns the
ESP32 WebSocket, exposes a local Streamable HTTP MCP endpoint, and routes
incoming `tools/call` requests through a bounded command queue.

## References checked

- Issue #178: open as of this spike. It asks for a single shared gateway
  daemon, HTTP-based MCP transport, command queue, and correlation-safe
  multi-client response routing.
- Issue #177: closed, with PR #254 merged into `main` on
  `2026-06-01T08:36:04Z`. Local branch base includes commit `c2328ac`
  (`fix(gateway): #177 ownership lock refuse-mode MVP (Windows-safe)`).
- Issue #73: open as of this spike. It recommends hardware-lane-aware
  parallelism as the principled answer, but the firmware-side outcome is not
  landed.
- Issue #169: treated as closed/completed and stabilized per maintainer note
  for this task (`2026-05-19`). This spike did not re-run `gh issue view 169`.
- MCP transport spec:
  <https://modelcontextprotocol.io/specification/2025-06-18/basic/transports>
- Legacy MCP HTTP+SSE transport spec:
  <https://modelcontextprotocol.io/specification/2024-11-05/basic/transports>
- MCP Python SDK server docs:
  <https://py.sdk.modelcontextprotocol.io/server/>
- Local MCP Python SDK source inspected:
  `gateway/.venv/lib/python3.14/site-packages/mcp/server/sse.py`
- Local MCP Python SDK source inspected:
  `gateway/.venv/lib/python3.14/site-packages/mcp/server/streamable_http.py`
- Local MCP Python SDK source inspected:
  `gateway/.venv/lib/python3.14/site-packages/mcp/server/streamable_http_manager.py`
- Local MCP Python SDK source inspected:
  `gateway/.venv/lib/python3.14/site-packages/mcp/client/streamable_http.py`
- Local MCP Python SDK source inspected:
  `gateway/.venv/lib/python3.14/site-packages/mcp/server/fastmcp/server.py`

Note: the draft MCP transport page was also checked during the spike, but it
contains draft/future transport revisions relative to this repo's current
implementation target. Phase B should re-check the stable spec immediately
before implementation starts.

SDK maturity note: the installed wheel does not include the upstream SDK test
suite, so this spike cannot make a line-by-line test coverage claim from local
files alone. The local SDK surface and official Python SDK docs both indicate
that Streamable HTTP is the actively recommended production path, while SSE is
kept as a supported but superseded transport.

## Transport comparison matrix

| Topic | Legacy HTTP+SSE | Streamable HTTP |
|---|---|---|
| Spec status | Defined by the `2024-11-05` transport spec. In the `2025-06-18` spec it is treated as the old transport to keep available only for older clients. | Current stable HTTP transport in the `2025-06-18` spec. The spec describes legacy HTTP+SSE compatibility as a fallback around it. |
| Endpoint shape | Two endpoints: a GET SSE endpoint plus a POST message endpoint. The server sends an `endpoint` SSE event so the client knows where to POST. | One MCP endpoint, typically `/mcp`. Every client JSON-RPC message is an HTTP POST. The server may answer with JSON or a request-scoped SSE stream. |
| Bidirectional support | Server-to-client messages flow over a long-lived SSE stream. Client-to-server messages flow over POST. This works, but the connection shape is split across two endpoints. | Client-to-server messages are POSTs. Server responses can be JSON or SSE streams. Optional GET SSE can carry server-initiated messages where needed. |
| Reconnect semantics | The local SDK's `SseServerTransport` keeps in-memory session writers keyed by query-string session IDs. It does not provide an event-store abstraction for resumability. A lost SSE stream is effectively a lost in-memory session. | The local SDK includes `StreamableHTTPServerTransport`, `StreamableHTTPSessionManager`, optional `EventStore`, `Last-Event-ID` handling, session IDs via `Mcp-Session-Id`, and client reconnection support. |
| Multi-client routing | Possible, but routing depends on per-SSE-session query parameters and a separate POST endpoint. This is enough for simple clients, but awkward for a daemon that wants clear queue ingress and audit boundaries. | Built for multiple HTTP clients. Stateful mode gives each client an `Mcp-Session-Id`; stateless mode can still route each request by JSON-RPC `id`. Both map directly to command-queue correlation IDs. |
| Backpressure and queue ingress | The SDK transport uses zero-buffer memory streams internally. Backpressure policy for the ESP32 command queue would be entirely external, and errors would have to be fitted around the old two-endpoint flow. | Each POST is a natural queue ingress point. The daemon can reject saturated `tools/call` requests with a JSON-RPC error before enqueueing, or hold the HTTP response open until the queued command completes. |
| Python SDK server support | `SseServerTransport` and `FastMCP.run(transport="sse")` exist. Python SDK docs say SSE is being superseded by Streamable HTTP. | `FastMCP.run(transport="streamable-http")`, `streamable_http_app()`, `StreamableHTTPSessionManager`, JSON response mode, stateless mode, stateful sessions, and optional event-store resumability are present in local `mcp==1.27.0`. |
| Security fit | The SDK supports transport security middleware, but the split endpoint model leaves more endpoint surface to document and protect. | Same transport security middleware, fewer MCP endpoints, and a clearer place to enforce bearer-token authentication, host/origin checks, and queue ingress limits. |
| Pros for this gateway | Older-client compatibility and conceptually simple SSE stream. | Current spec direction, current SDK direction, simpler daemon endpoint, clearer per-request response routing, better room for resumability and future server notifications. |
| Cons for this gateway | Superseded, two-endpoint flow, weaker local resumability support, and less aligned with future MCP clients. | Slightly more moving parts in the SDK and one Phase B choice remains: stateful sessions vs. stateless JSON responses. |

## Recommendation

Use **Streamable HTTP** for Phase B.

The daemon should expose a Streamable HTTP MCP endpoint and keep the current
stdio gateway available as a legacy/debug path during the migration window.
Do not build the new daemon on the legacy HTTP+SSE transport unless a specific
target client cannot speak Streamable HTTP.

For the first implementation PR, prefer the MCP Python SDK's existing
Streamable HTTP server machinery over a custom transport:

- Low-level route: `StreamableHTTPSessionManager` plus the existing MCP server
  object if the current `stdio_server.py` structure is kept.
- FastMCP route: `FastMCP.streamable_http_app()` if Phase B first migrates the
  gateway tool declarations onto FastMCP.

The recommendation is not based on ease alone. Streamable HTTP is the better
fit because it gives the gateway a single queue ingress endpoint, a spec-level
session/correlation model, SDK support for JSON or SSE responses, and a path to
resumability without inventing a custom transport.

### Open questions before Phase B

1. Should the daemon run Streamable HTTP in stateful mode, stateless JSON mode,
   or start stateful and later evaluate stateless?
2. What port and CLI shape should be used? A reasonable sketch is
   `stackchan-mcp serve --transport streamable-http` with
   `MCP_HTTP_HOST=127.0.0.1` and `MCP_HTTP_PORT=8767`.
3. Should HTTP MCP auth be mandatory even when bound to localhost? The safe
   default is to require `Authorization: Bearer <STACKCHAN_TOKEN>` for all
   `/mcp` traffic when a token is configured, and to refuse non-loopback binds
   without a token.
4. What queue limit and error code should be standardized for saturation?
5. Which tools may bypass or parallelize with the queue? Until #73 lands, only
   daemon-local health/status reads should bypass the ESP32 command queue.
6. How long should stdio remain supported after the daemon path ships?

## API sketch

This is pseudo-code only. It is not intended to be copied into the gateway as
Python.

### Process shape

```text
stackchan-mcp serve
  load .env
  acquire #177 ownership lock once for the daemon process
  start ESP32 WebSocket listener on HOST:WS_PORT (default 0.0.0.0:8765)
  start capture server on HOST:CAPTURE_PORT (default 0.0.0.0:8766)
  start Streamable HTTP MCP server on MCP_HTTP_HOST:MCP_HTTP_PORT
  keep running after HTTP MCP clients disconnect
  release ownership lock only when the daemon exits
```

### Endpoints

```text
POST /mcp
  MCP Streamable HTTP endpoint.
  Requires Authorization: Bearer <token> when STACKCHAN_TOKEN or BEARER_TOKEN
  is configured.
  Accept: application/json, text/event-stream
  Content-Type: application/json

GET /healthz
  Local operational health.
  Returns process state, ESP32 WebSocket state, queue depth, and owner_id.
  Does not dispatch to the ESP32.

GET /status
  Authenticated status endpoint.
  Returns the same logical state as the get_status MCP tool, plus queue depth
  and connected client/session count.
```

### Tool invocation routing

```text
on HTTP POST /mcp:
  validate Host and Origin for DNS rebinding protection
  validate Authorization bearer token
  parse JSON-RPC message

  if method is initialize/tools/list:
      answer through SDK transport

  if method is tools/call:
      client_session_id = request.headers["Mcp-Session-Id"] if present
      client_request_id = jsonrpc.id
      correlation_id = uuid4()

      if queue is full:
          return JSON-RPC error to client_request_id:
              code = -32000
              message = "stackchan command queue is full"
              data = { "correlation_id": correlation_id, "queue_depth": N }

      enqueue:
          correlation_id
          client_session_id
          client_request_id
          tool name
          arguments
          response future

      await response future
      return JSON-RPC response using the original client_request_id
```

### Dispatcher

```text
while daemon is running:
  item = await queue.get()

  if ESP32 is not connected:
      complete item with MCP error "No ESP32 device connected"
      continue

  dispatch one ESP32-bound command at a time by default
  preserve original per-client order naturally through FIFO ordering
  complete only the response future attached to that correlation_id
```

## Compatibility check: #177 ownership lock

#177 is merged in the local base used for this spike. The current gateway now
has:

- `gateway/stackchan_mcp/ownership.py`
- `generate_owner_id()`
- `acquire_lock(owner_id)`
- `release_lock()`
- a lock file at `~/.stackchan-mcp/owner.lock`
- CLI acquisition before the gateway starts
- `stackchan-mcp --check` for lock inspection

The ownership lock still applies in daemon mode. The daemon should acquire the
lock once at process startup because it is the single process that owns the
ESP32 WebSocket. HTTP MCP clients connecting to that daemon must not acquire or
release the ownership lock individually. Client disconnects should leave the
daemon and the lock alive; only daemon shutdown should release the lock.

No fundamental `ownership.py` redesign is required for Phase B. The likely API
additions are small:

- include daemon-mode metadata in the lock file if useful for diagnostics
  (`mode`, `http_endpoint`, maybe `started_by`);
- keep `--check` able to show the daemon endpoint;
- avoid using #177 queue/preempt lock modes as the #178 multi-client queue.
  The #178 queue lives inside the daemon after the lock has already been
  acquired.

## Compatibility check: #169

For this spike, #169 is treated as closed/completed and stabilized per the
maintainer note supplied with the task:

> Issue #169 is already CLOSED/COMPLETED as of 2026-05-19.

That means Phase B does not need compensating firmware work solely for #169.
The #178 daemon can assume the firmware-side WebSocket/audio-state separation
is stable enough for design purposes.

Phase B should still do a final real-device verification before merge:

- ESP32 WebSocket remains persistent while the device moves between idle,
  listening, speaking, and capture states.
- A daemon restart and ESP32 reconnect do not accidentally force audio/listen
  state transitions.
- Long-running TTS or listen flows do not block unrelated daemon health checks.

If that verification fails, #178 implementation should pause rather than hide
firmware coupling inside the HTTP daemon.

## Compatibility check: #73

#73 is open. It recommends hardware-category tags/lane-aware parallelism as
the principled answer, with compound tools or background tasks as pragmatic
shortcuts.

Current gateway code already has per-lane locks in `ESP32Manager` for grouped
gateway calls, but #73 is about the on-device dispatcher and user-visible
parallelism across hardware-independent tools. Because #73 is not resolved,
the Phase B command queue should start conservative:

- default to a single FIFO queue for all ESP32-bound tool calls;
- preserve FIFO ordering across all clients;
- allow daemon-local health/status reads to bypass the ESP32 queue only when
  they do not call into the device;
- do not introduce priority ordering in Phase B unless the maintainer explicitly
  needs emergency-stop semantics;
- do not add new cross-lane parallel dispatch in the daemon until #73 defines
  the safe firmware-side policy.

After #73 lands, the queue can evolve from:

```text
global FIFO -> per-hardware-lane queues with a shared WS serializer or
               compound-command scheduler, depending on the firmware outcome
```

## Command queue design sketch

### Default policy

Use a bounded global FIFO queue across all HTTP MCP clients.

This is the simplest policy that satisfies #178's main path:

- multiple clients can connect concurrently;
- exactly one daemon owns the ESP32 WebSocket;
- each tool result is routed back to the original client request;
- the ESP32 is not asked to handle unsafe parallel command streams before #73
  is settled.

### Backpressure

The queue must be bounded. A reasonable first default is a small configurable
limit such as `STACKCHAN_COMMAND_QUEUE_SIZE=32`.

When the queue is full:

```json
{
  "code": -32000,
  "message": "stackchan command queue is full",
  "data": {
    "queue_depth": 32,
    "retry_after_ms": 250
  }
}
```

The exact error code can be finalized in Phase B. The important rule is: do
not buffer unbounded commands for hardware.

### Per-client response routing

Each queued item should carry:

- `correlation_id`: daemon-generated UUID for logs and queue tracking;
- `client_session_id`: `Mcp-Session-Id` when using stateful Streamable HTTP;
- `client_request_id`: original JSON-RPC request ID;
- `tool_name`;
- `arguments`;
- `response_future`;
- `enqueued_at`.

The dispatcher completes only the `response_future` associated with that
correlation ID. The HTTP handler then returns the JSON-RPC result using the
original client request ID.

### Priority queue

Do not start with a priority queue.

Priority ordering creates user-visible fairness questions immediately: a
dashboard polling status could starve an interactive client, or an automation
could jump ahead of a user sitting beside the robot. FIFO is easier to explain
and debug for this single-user LAN product.

The only priority-like behavior worth considering later is a dedicated
emergency/stop lane, and only if the firmware exposes a safe operation that is
defined to interrupt or supersede queued motion/audio.

## Phase B implementation estimate

Rough follow-up PR size:

- 1 new daemon CLI/subcommand path in `gateway/stackchan_mcp/cli.py`;
- 1 HTTP MCP server module or FastMCP adapter module;
- 1 command queue module;
- small ownership lock diagnostics updates;
- tests for queue ordering, queue-full errors, auth rejection, and response
  correlation;
- docs updates for daemon setup and stdio migration.

Expected size: about 4-7 gateway files, 300-700 lines of implementation, plus
tests and docs.

Expected duration: one to two focused weeks if Streamable HTTP is integrated
through the existing SDK APIs; longer if the current stdio tool registration
needs to be refactored before it can be shared with the HTTP daemon.
