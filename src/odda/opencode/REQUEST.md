# Raw request crafting

`odda request` lets you craft and send raw HTTP requests byte-for-byte, bypassing the browser. Use it to replay/modify captured flows or send hand-built requests for header-injection, smuggling, and parser-differential tests. This is the reference for the `request` sub-app; see [SKILL.md](SKILL.md) for the quick-reference table and Targeting model.

## File layout

Editable requests live in `.odda/requests/<name>/`:

- `request` — the raw HTTP request bytes (request line + headers + blank line + body), **CRLF-terminated**, same format as `.odda/flows/<id>/request`. For small edits (tweaking a header value) use the built-in edit tool; for full-request rewrites or binary bodies use shell (`printf '...\r\n...'` or `cat`) — **ensure `\r\n` line endings** either way (heredocs use `\n` which will fail on the wire).
- `meta.json` — sidecar with `{"scheme": "http"|"https", "host": "...", "port": N}`. `send` uses this to open the socket; the `request` file is origin-form and carries no scheme/port. The `host` here is the TCP destination — it may intentionally differ from the `Host` header in the request file (for vhost/host-header/SSRF tests).

## Commands

- `odda request clone --flow-id <flow-id> --name <name> [--force]` — Copy `.odda/flows/<flow-id>/request` into `.odda/requests/<name>/request` and copy the flow's `meta.json` sidecar (scheme/host/port) into the editable request's `meta.json`. The `Host` header in the request file is left untouched and goes on the wire verbatim (it may intentionally differ from `meta.json`'s `host` for vhost/host-header/SSRF tests). Errors if the flow has no `meta.json` sidecar (older captures — re-capture). Refuses to overwrite an existing request unless `--force`.
- `odda request new --name <name> --host <host> [--protocol http|https] [--port <port>] [--force]` — Create an empty `request` file (0 bytes) and a `meta.json` with the given host, protocol (default `https`), and port (default 80 for `http`, 443 for `https`). Fill the `request` file with the edit tool.
- `odda request send --name <name> [--fix-content-length] [--timeout 30] [--insecure]` — Read both files, open a TCP socket (TLS for https, HTTP/2 when the request line says `HTTP/2`), write the exact bytes from the `request` file, read the response, decode it (de-chunk + gzip/br/deflate/zstd), and write a flow record to `.odda/flows/<NNNNN>/`. The sent request is recorded before the network exchange (two-phase durability), so a crash leaves a durable request file. Output is the `flows.jsonl` record that was appended; read `.odda/flows/<id>/response_body.*` for the body.

## `send` flags

- `--fix-content-length` — Recompute `Content-Length` from the body and overwrite the header (the `request` file on disk is untouched). Use this when you've edited the body and want the framing auto-corrected. Skip it for Content-Length smuggling/differential tests where the wrong value is the point.
- `--timeout <seconds>` — Total timeout for connect + reads (default 30). On timeout, a flow record is written with whatever was received plus an `error` file.
- `--insecure` — Skip TLS certificate verification. Default verifies. Verification keys off `meta.json`'s `host` (the TCP destination / SNI), **not** the `Host` header — so `Host: localhost` with `meta.json` pointing at the real host will verify fine without `--insecure`.

## Behavior notes

- **Single-shot, no redirects.** A 3xx response is recorded as-is; re-`send` manually if you want to follow.
- **`send` re-reads the `request` file at call time.** You may overwrite it freely between sends (e.g. `printf > .odda/requests/<name>/request` then `send`, then overwrite and `send` again). Only `clone`/`new` refuse to overwrite without `--force`.
- **No pre-flight validation.** Malformed requests fail at the socket/TLS/H2 layer; the error is captured in the flow's `error` file.
- **HTTP/2** — if the request line says `HTTP/2`, `send` speaks HTTP/2 on the wire (pseudo-headers synthesized from the request line + `Host` + `meta.json`). The stored `request` file stays H1-shaped text with `HTTP/2` in the version field (consistent with how captured H2 flows are stored). If the server doesn't speak HTTP/2, `send` errors — edit the request line to `HTTP/1.1` and resend.
- **Missing framing** — if a body exists with no `Content-Length` and no `Transfer-Encoding: chunked`, `send` half-closes the socket (`write_eof`) after the body so the server sees EOF.
- **Binary bodies** — the `request` file is bytes; populate it via shell (`cat`, `cp`) if the edit tool can't author the bytes you need.
- **Captured-sent requests are stored in the same `flows.jsonl`** as proxied captures, with `scheme` and `port` fields populated. `flows.jsonl` records from older captures may lack these fields; treat them as `https`/`443` when absent. See [FLOWS.md](FLOWS.md) for the `flows.jsonl` schema.