# WebSocket messages capture to a per-flow sidecar, latin-1-escaped, invisible to the scanner

WebSocket traffic through the proxy captures as data, passively: mitmproxy
(12.x) represents a WebSocket as an `HTTPFlow` with `.websocket` set, and
odda's `FlowFileAddon` now handles the `websocket_message` and
`websocket_end` hooks. The 101 handshake records exactly like any other
flow — one `flows.jsonl` line, one flow directory — except the line carries
`"ws": true`. Each message appends one JSON line to the flow's sidecar,
`flows/<id>/ws.jsonl`: `{"from": "client"|"server", "opcode":
"text"|"binary", "content": <str>, "ts": <float>}`, ending in one close
record `{"type": "close", "code", "reason", "closed_by_client"}` after
which the sidecar is sealed read-only (the same `0444` mode as every
stored file). Uncapped, like HTTP response bodies.

## Decisions

**Sidecar, not index lines.** The rejected alternative appended one
`flows.jsonl` line per message (kind-tagged, parent-referencing). Rejected
because chatty sockets — the interesting ones — would drown the one
append-only index everything reads: agent tailing, `wait_flow`-style
marker greps (a message line can contain the handshake's marker), and any
future tooling that assumes one line per exchange. The sidecar keeps the
index one-line-per-flow forever. The cost is discovery, paid once with
the static `"ws": true` field on the handshake record: mitmproxy sets
`flow.websocket` *before* the response hook fires (`send_response` in
`proxy/layers/http/__init__.py`), so the flag is written once at
handshake time from mitmproxy's own ground truth — not the `101`
heuristic, which lies for any non-WebSocket protocol upgrade. Agents
reading only the index learn sockets exist; the sidecar tells the rest.

**Latin-1 escape, not base64.** Binary frames store
`bytes.decode("latin-1")`. The mapping is bijective (byte ↔ U+0000–U+00FF),
so every payload round-trips exactly via `content.encode("latin-1")`.
Measured against base64 (house `ensure_ascii=False` JSON): parity to
1.14× on dense random bytes, *smaller* on ASCII-heavy binary — and the
point of capture survives intact: a JWT prefix embedded in a msgpack
frame stays greppable (`rg 'eyJ'` hits the sidecar line), where base64
would make it invisible. Base64 was rejected as the data dir's first
grep-opaque format. Text frames are raw UTF-8 (`decode("utf-8",
"replace")`, lenient like the scanner's `FlowView`).

**Invisible to the scanner.** The ADR-0034 hook fires on index appends;
message lines ride no append, so no rule ever sees a WebSocket message.
Excluded deliberately (the alternative — synthetic index records or a
second observer seam — buys rule coverage nobody asked for at the price
of a second ingestion path). If WS scanning is ever wanted, the sidecar
is the natural seam to extend; nothing in this design blocks it.

**Passive only.** No message injection, no MCP read tool: agents read
the data dir like every other capture surface. Frame injection is a
separate future surface (it mutates live sockets and needs its own ADR).

## Costs accepted

- `flows.jsonl` consumers learn a new key (`ws`) on 101 records —
  additive; existing readers ignore unknown keys (verified: the scanner's
  `FlowView` reads a fixed field set, `request` clone reads `meta.json`).
- RFC 8441 (WebSocket over HTTP/2 extended CONNECT) is unsupported by
  mitmproxy 12; through odda's explicit `http://` proxy the browser path
  is HTTP/1.1 CONNECT + h1 upgrade, which is supported and e2e-tested.
  Origins speaking *only* RFC 8441 would fail — accepted, documented.
- A socket's sidecar is writable until close; a session killed mid-socket
  leaves an unsealed `ws.jsonl` without a close line (end-of-file then
  means "over", not "clean").
