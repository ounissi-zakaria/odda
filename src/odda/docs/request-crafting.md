# Raw request crafting

The `request_*` tools let you craft and send raw HTTP requests byte-for-byte, bypassing the browser. Use them to replay/modify captured flows or send hand-built requests for header-injection, smuggling, and parser-differential tests.

## File layout

Editable requests live in `.odda/requests/<name>/`:

- `request` — the raw HTTP request bytes (request line + headers + blank line + body), **CRLF-terminated**, same format as `.odda/flows/<id>/request`. For small edits (tweaking a header value) use your file-editing tool; for full-request rewrites or binary bodies use shell (`printf '...\r\n...'` or `cat`) — **ensure `\r\n` line endings** either way (heredocs use `\n` and will fail on the wire).
- `meta.json` — sidecar with `{"scheme": "http"|"https", "host": "...", "port": N}` and an optional `line_terminator` (default `"\r\n"`, see [Line terminator](#line-terminator-h2-only)). `request_send` uses this to open the socket; the `request` file is origin-form and carries no scheme/port. The `host` here is the TCP destination — it may intentionally differ from the `Host` header in the request file (for vhost/host-header/SSRF tests). For HTTP/2, the `Host` header is odda's input syntax for the `:authority` pseudo-header (RFC 9113 §8.3.1 forbids `Host` in H2; odda translates `Host` → `:authority` at send time); `meta.json`'s `host` still controls the TCP/SNI destination.

## Tools

- `request_new(name, host, protocol="https", port=None, line_terminator=None, force=False)` — Create an empty `request` file (0 bytes) and a `meta.json` with the given host, protocol, and port (default 80 for `http`, 443 for `https`). `line_terminator` is a byte list the parser splits header lines on (default `[13, 10]`, i.e. `\r\n`); see [Line terminator](#line-terminator-h2-only). Fill the `request` file with your file-editing tool.
- `request_clone(flow_id, name, force=False)` — Copy `.odda/flows/<flow_id>/request` into `.odda/requests/<name>/request` and copy the flow's `meta.json` sidecar (scheme/host/port) into the editable request's `meta.json`. The `Host` header in the request file is left untouched and goes on the wire verbatim (it may intentionally differ from `meta.json`'s `host` for vhost/host-header/SSRF tests). Errors if the flow has no `meta.json` sidecar (older captures — re-capture). Refuses to overwrite an existing request unless `force`.
- `request_send(name=None, names=None, repeat=None, fix_content_length=False, timeout=30, insecure=False, pipelining=False)` — Read both files, open a TCP socket (TLS for https, HTTP/2 when the request line says `HTTP/2`), write the exact bytes from the `request` file, read the response, decode it (de-chunk + gzip/br/deflate/zstd), and write a flow record to `.odda/flows/<NNNNN>/`. The sent request is recorded before the network exchange, so a crash leaves a durable request file. Returns the `flows.jsonl` record that was appended; read `.odda/flows/<id>/response_body.*` for the body. The response body is **always stored**, even when `Content-Type` is in the set (images/video/audio/fonts) that browser-capture drops — a hand-built request exists to see its response body (e.g. a path-traversal file mislabeled `image/jpeg`).

## Multi-name send (`names`)

Pass two or more names in `names` to send two or more editable requests on **one connection**. The mechanism is chosen by the request line: HTTP/1.1 request lines use one H1 connection with **sequential keep-alive** (the Multi-name pipeline — response-queue poisoning, same-connection CL.0 confirmation, H1 request smuggling); HTTP/2 request lines use one H2 connection with **concurrent stream-multiplex** (the multi-endpoint race path — see "Race conditions / concurrent send" below). Mixed H1+H2 request lines in one multi-name send error pre-emptively (different mechanisms on one connection).

- **Output.** One flow record per request, all on one connection: a **list** of flow records, in send order.
- **Same `timeout` semantics.** One deadline for connect + all sends + all reads. Pick a generous total for slow smuggling responses.
- **Pre-emptive rejections** (before the socket opens):
  - `fix_content_length` + multi-name → error (it would overwrite the intentionally-wrong `Content-Length` that smuggling payloads depend on).
  - Mixed HTTP/1.1 + HTTP/2 request lines → error (H1 multi-name is sequential keep-alive; H2 multi-name is concurrent stream-multiplex — different mechanisms).
  - `pipelining` + H2 multi-name → error (`pipelining` is H1-only send-all-then-read-all; H2 multi-name is already concurrent).
  - Any H1 name with a body but no `Content-Length` and no `Transfer-Encoding` → error (it would require half-closing the socket, ending the connection). Single-name send still allows this via EOF; multi-name H1 does not. H2 multi-name uses stream framing, so a body without CL/TE is fine.
- **Mid-sequence failure (H1 sequential).** If step N fails (timeout, connection drop), step N gets today's error flow; the remaining steps are recorded as `aborted: step N failed (...)` error flows. The connection closes. `flows.jsonl` stays complete — every request has a line.
- **Per-stream failure (H2 concurrent).** Per-stream errors are isolated (one stream's RST does not abort the others); a connection-level error (GOAWAY, TLS drop) aborts all not-yet-complete flows. See "Race conditions / concurrent send" for the full error policy.

## `pipelining` (multi-name only)

`pipelining` switches from sequential keep-alive (send-then-read per request, the default) to true H1 pipelining (send all requests, then read all responses). Which one you want depends on where the smuggle's effect lives:

- **Leftover-prefix smuggling (CL.TE, TE.CL, CL.0)** — the smuggle ends its body and leaves a literal prefix in the back-end's buffer that prefixes the *next* request's method. Use **sequential keep-alive (`pipelining` off)**: a second request on the same connection (the second entry in `names`, or a fresh single-name send — "the next request" from here on) arrives after the smuggle and gets prefixed.
- **Still-parsing / victim-consumption** — the victim must arrive *while* the server is still parsing the smuggling POST's body, so it gets consumed as body bytes. Use **`pipelining`**: all requests go on the wire up front, so the victim is in flight before the body parse completes.

Sequential keep-alive also covers response-queue poisoning and same-connection CL.0 confirmation. A single-name `request_send` (no victim) may suffice when the back-end surfaces the smuggled method on its own response — e.g. a `0\r\n\r\nG` body turns the next method into `GPOST`, and if the server echoes `"Unrecognized method GPOST"` on the smuggle's own response, one send confirms it. Send the next request only when the back-end buffers the prefix for a *later* victim rather than echoing it.

**Where the interesting response lands.** In leftover-prefix smuggling the response queue desyncs from the request queue, so the interesting response (the smuggled method's error or confirmation) lands on the **smuggle** flow's record, not the victim's. The victim flow may show `status_code: null` with an `error` (e.g. `"aborted: step N failed (connection closed before response headers)"`) and no body — that's expected, not a failure signal. Read both flows before concluding the smuggle did nothing.

## Race conditions / concurrent send (`repeat` and H2 multi-name)

For race-condition / limit-overrun attacks, you need N requests to arrive at the server near-simultaneously so they all slip through a check-then-write window. The sequential multi-name pipeline and `pipelining` are the wrong primitive — they're server-side sequential (only the first request lands in the window). Use **concurrent send**:

- **`repeat` (with a single `name`)** — one request, N concurrent copies. This matches the limit-overrun and rate-limit-bypass pattern (the same request fired many times at once). HTTP/2 uses stream multiplexing with the last-byte single-packet technique (all N HEADERS frames in one TLS record) so the requests arrive atomically; HTTP/1.1 opens N parallel connections (H1 cannot multiplex on one connection, and pipelining is server-side sequential).
- **Multi-name over HTTP/2 (`names` with `HTTP/2` request lines)** — N different requests fired concurrently as H2 streams on one connection. This matches the multi-endpoint race pattern (N different endpoints raced to exploit a state-machine race). H1 multi-name stays sequential (smuggling).

Both produce one flow record per request (the one-request-one-response model is preserved). Count successes by filtering `flows.jsonl` by the race's host/path and counting `status_code` (e.g. count of 302 "applied" vs 200 "already applied").

`repeat` is single-name only (`repeat` + `names` errors — the combo is ambiguous). `repeat` + `pipelining` errors (`repeat` is concurrent; `pipelining` is H1 sequential). `fix_content_length` + `repeat` is allowed (recompute CL once, fire N copies; the race is never a CL-differential attack). `pipelining` + H2 multi-name errors (`pipelining` is H1-only).

Error policy: one `timeout` for the whole concurrent send. Per-stream errors are isolated (one H2 stream's RST does not abort the others — H2 streams are independent); a connection-level error (GOAWAY, TLS drop) aborts all not-yet-complete flows. All N request files are pre-written before the socket opens (two-phase durability); `flows.jsonl` stays complete.

**When to use `repeat` vs `eval` with `Promise.all(fetch)`:** `repeat` is out-of-page (raw bytes, no browser cookies — the Burp-Repeater model; clone a captured flow to carry the auth cookie in the request bytes). Use `eval` running `Promise.all(Array(N).fill().map(()=>fetch(url,{...})))` from the page when the request is cookie-gated and you want the browser to carry the session automatically (the browser's HTTP/2 multiplexing also delivers near-simultaneously, though without the single-packet guarantee). Note: concurrent `eval` calls on the same tab **serialize** — "race harder by firing parallel evals" silently does nothing; use one `eval` with `Promise.all` for concurrency.

## `request_send` params

- `fix_content_length` — Recompute `Content-Length` from the body and overwrite the header (the `request` file on disk is untouched). Use this when you've edited the body and want the framing auto-corrected. Skip it for Content-Length smuggling/differential tests where the wrong value is the point. Single-name only; rejected with multi-name.
- `timeout` — Total timeout for connect + reads (default 30). On timeout, a flow record is written with whatever was received plus an `error` file. In multi-name mode, one deadline covers the whole pipeline (connect + all sends + all reads).
- `insecure` — Skip TLS certificate verification. Default verifies. Verification keys off `meta.json`'s `host` (the TCP destination / SNI), **not** the `Host` header — so `Host: localhost` with `meta.json` pointing at the real host will verify fine without `insecure`.
- `pipelining` — Multi-name H1 only. Send all requests then read all responses (true H1 pipelining) instead of send-then-read per request (default sequential keep-alive). See "Multi-name pipeline" above. Rejected with H2 multi-name (H2 multi-name is already concurrent) and with `repeat`.
- `repeat` — Single-name only. Send N concurrent copies of the request (race / limit-overrun). `>=2` enables concurrent send (H2 stream-multiplex with last-byte single-packet, or H1 parallel connections). See "Race conditions / concurrent send" above. Rejected with multiple names (ambiguous combo) and with `pipelining`. `fix_content_length` + `repeat` is allowed.

## Behavior notes

- **Single-shot, no redirects.** A 3xx response is recorded as-is; re-send manually if you want to follow.
- **`request_send` re-reads the `request` file at call time.** You may overwrite it freely between sends (e.g. `printf > .odda/requests/<name>/request` then `request_send`, then overwrite and send again). Only `request_clone`/`request_new` refuse to overwrite without `force`.
- **No pre-flight validation.** Malformed requests fail at the socket/TLS/H2 layer; the error is captured in the flow's `error` file.
- **HTTP/2** — if the request line says `HTTP/2`, `request_send` speaks HTTP/2 on the wire (pseudo-headers synthesized from the request line + `Host` + `meta.json`). The stored `request` file stays H1-shaped text with `HTTP/2` in the version field (consistent with how captured H2 flows are stored). `Host` becomes the `:authority` pseudo-header (RFC 9113 §8.3.1 forbids `Host` in H2); `meta.json`'s `host` controls the TCP/SNI destination. For vhost/host-header labs where the TCP destination and the routing header must disagree, use HTTP/1.1 (`HTTP/1.1` in the request line) — there `meta.host` is the TCP/SNI destination and `Host` goes on the wire verbatim, independently. If the server doesn't speak HTTP/2, send errors — edit the request line to `HTTP/1.1` and resend.
- **Missing framing** — if a body exists with no `Content-Length` and no `Transfer-Encoding: chunked`, send half-closes the socket after the body so the server sees EOF.
- **No trailing CRLF on the body.** CRLF terminates header lines; the body is raw bytes after the blank line. The natural `printf 'headers...\r\n\r\nbody\r\n'` recipe (mirroring how the header block is written) appends a trailing `\r\n` that becomes body bytes, corrupting URL-encoded form values (e.g. `stockApi=...value\r\n` — the server sees a stray newline in the value). Write `printf 'headers...\r\n\r\nbody'` (no trailing `\r\n` after the body). `fix_content_length` counts the trailing bytes and sends them faithfully, so it won't save you here.
- **Chunked bodies must terminate with `0\r\n\r\n`.** A chunked body ends with a zero-length chunk (`0\r\n`) plus a trailing blank line (`\r\n`); omitting the final `\r\n` produces a generic 400 Bad Request with no hint that the chunked framing was malformed. Smuggling payloads may intentionally malform or omit the terminator to desync the front-end's chunked parser from the back-end — see the `pipelining` section above for the smuggling context.
- **Binary bodies** — the `request` file is bytes; populate it via shell (`cat`, `cp`) if your file-editing tool can't author the bytes you need.
- **Captured-sent requests are stored in the same `flows.jsonl`** as proxied captures, with `scheme` and `port` fields populated. `flows.jsonl` records from older captures may lack these fields; treat them as `https`/`443` when absent. See `odda://docs/flows` for the `flows.jsonl` schema.

## Line terminator (H2 only)

`line_terminator` (a list of byte ints) sets the byte sequence the request parser splits header lines on (default `\r\n` = `[13, 10]`). The block terminator (header/body separator) is always two line terminators. Pass it to `request_new`; it is stored in `meta.json` as `line_terminator` (a list of byte ints) and round-trips on send.

**H2-only.** The line terminator is honored only for HTTP/2 request files. HTTP/1.1 request files are wire-faithful — the bytes go on the socket verbatim, so re-framing them is meaningless. Setting it on an H1 request has no effect (the parser always uses `\r\n` for H1).

**Why.** HTTP/2 uses binary length-prefixed framing — a CRLF inside a header value does not split anything on the wire. Some gateways accept H2 from the client and translate to H1 for the back-end (H2→H1 downgrade). If the H2 `:path` or `:authority` value contains a literal `\r\n`, the gateway emits an H1 request line/headers where the CRLF splits into a smuggled header or request on the back-end. To craft such an H2 request, the parser must not split on the CRLF you intend to inject — so you pick a line terminator that does not appear inside any value.

**Recipe (H2→H1 downgrade smuggling via `:path`).** Set the line terminator to a byte that won't appear in your values (e.g. `\x00`), so a full `\r\n` inside the path survives the parse into the H2 frame:

```bash
request_new(name="h2smuggle", host="<lab>", port=443, line_terminator=[0])
printf 'GET /foo\r\nX-Evil:yes HTTP/2\x00Host: <lab>\x00\x00' \
  > .odda/requests/h2smuggle/request
request_send(name="h2smuggle", insecure=True)
```

The `:path` sent in the H2 frame is `/foo\r\nX-Evil:yes`. An H2→H1 downgrade gateway that translates `:path` into the H1 request line emits `GET /foo\r\nX-Evil:yes HTTP/1.1\r\n...` — splitting `X-Evil:yes` into a smuggled header on the back-end. The injected portion must not contain the line terminator (`\x00`) and must not contain a space (the request line is split on spaces to find method/path/version); use no-space header syntax (`X-Evil:yes`, not `X-Evil: yes`).

**Constraint.** No value (and no path) in the request file may contain the line terminator — the parser splits on it. Pick a terminator that does not appear in any value you craft (like picking a here-doc delimiter).