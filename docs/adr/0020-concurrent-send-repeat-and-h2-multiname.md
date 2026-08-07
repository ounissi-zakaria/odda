# `request send` gains concurrent send: `--repeat N` and H2 multi-name (stream multiplex)

ADR-0019 rejected H2 stream multiplexing in the multi-name pipeline as
"scope creep for a non-use-case." The PortSwigger race-conditions lab run
(35 labs, lab 26 limit-overrun) produced that use-case: there is no
first-class way to send N copies of one request concurrently to race a
server-side check-then-write window. The only working path was `odda eval`
running `Promise.all` of `fetch()` from the page (the browser's H2
multiplexing), which is an undocumented workaround for a gap in the raw
request tool, and which itself has a footgun (concurrent `eval` calls on
the same tab serialize — "race harder by firing parallel evals" silently
does nothing).

This ADR supersedes the H2-rejection clause of ADR-0019. The sequential
clauses of ADR-0019 (single-name frozen contract; multi-name H1
sequential keep-alive; `--pipelining`; the `--fix-content-length` +
multi-name rejection; the bare-body rejection; the mid-sequence error
policy) remain in force.

## Decisions

**1. `--repeat N` adds same-request-N-times concurrent send.** One
`--name`, `--repeat 20` fires 20 copies of that one request concurrently.
This matches the race / limit-overrun pattern exactly (the tested lab
fired 20 copies of one coupon-apply POST) and matches Turbo Intruder's
`count` model. `--repeat` is single-name only: `--repeat N --name a --name b`
errors pre-emptively (the combo is ambiguous — N-per-name vs N-total — and
the two shapes serve different use-cases; pick one per invocation).

**2. H2 multi-name is now concurrent (stream multiplex), not rejected.**
`--name a --name b ...` where any request line says `HTTP/2` now opens one
H2 connection and sends all N as concurrent streams (previously this
errored). This covers the multi-endpoint race pattern (N *different*
requests fired concurrently to exploit a state-machine race). H1
multi-name stays sequential keep-alive / `--pipelining` (frozen).

**3. H2 uses the last-byte single-packet technique.** All N streams'
HEADERS frames go out in one TLS record (one `data_to_send` flush) so
they arrive at the server near-simultaneously — the single-packet attack
(James Kettle, PortSwigger) that makes limit-overrun reliable rather
than flaky. For bodies ≥ 1 byte: `send_headers(end_stream=False)` for all
streams, then `send_data(body[:-1], end_stream=False)` for all, then one
final flush of `send_data(body[-1:], end_stream=True)` for all. Empty
bodies (GET/HEAD) send `send_headers(end_stream=True)` in the first
flush (no DATA frame; still in the single first-flush TLS record). This
matches Turbo Intruder's `Engine.BURP2`.

**4. H1 `--repeat` opens N parallel connections.** H1 cannot
stream-multiplex on one connection, and H1 pipelining is server-side
sequential (the report confirmed only the first request lands in the
race window). So H1 `--repeat` opens N parallel TCP/TLS connections in
the same process (`asyncio.gather`) and fires one request on each —
tighter than bash-parallel (no ~100-300ms process startup spread) but
each connection does its own TLS handshake so there's residual handshake
spread. This matches Burp's "send group in parallel." H1 multi-name
stays sequential (frozen; smuggling).

**5. Error policy: per-stream isolated, one timeout, connection error
aborts.** One `--timeout` covers connect + all sends + all reads. A
per-stream error (H2 RST_STREAM, H1 single-connection drop in the
parallel set) is isolated: that one flow gets an error record, the
others continue. A connection-level error (H2 GOAWAY, TLS drop) aborts
all not-yet-complete flows. All N request files are pre-written before
the socket opens (ADR-0019's two-phase durability scaled to N);
`flows.jsonl` stays complete — N pre-written requests, N lines.

## Allowed and rejected flag combinations

- `--fix-content-length` + `--repeat` → **allowed**. Recompute CL once,
  fire N copies. ADR-0019's rejection was multi-name-specific (different
  requests might carry intentionally-wrong CLs for smuggling); a
  same-request-N-times race is never a CL-differential attack, so the
  rejection's rationale does not transfer.
- `--repeat` + multiple `--name` → **rejected** (ambiguous combo).
- `--repeat` + `--pipelining` → **rejected** (`--pipelining` is H1
  multi-name sequential; `--repeat` is concurrent).
- `--insecure` + `--repeat` → **allowed** (passes through to each
  connection).

## What stays frozen

- Single-name `send` (one `--name`, no `--repeat`) is byte-for-byte
  unchanged (ADR-0019).
- H1 multi-name sequential keep-alive and `--pipelining` are unchanged
  (ADR-0019).
- The one-request-one-response flow model is unchanged: `--repeat N`
  produces N flow records with N (identical for `--repeat`, distinct for
  multi-name) request files (ADR-0019).
- The `--fix-content-length` + multi-name rejection stays (ADR-0019).
- The bare-body rejection (a body with no `Content-Length` and no
  `Transfer-Encoding`) stays in force for **H1** multi-name (it requires
  `write_eof`, ending the H1 connection). H2 multi-name uses stream
  framing, so a bare body is fine over H2 — the H2 carve-out is new
  here (ADR-0019 applied the rejection to all multi-name; this ADR
  narrows it to H1 multi-name only).

## Why not just document `eval Promise.all(fetch)`

The ticket originally proposed a one-line docs note pointing at `eval`.
That leaves the raw-request tool without a race primitive, sends agents
to an in-page workaround for an out-of-page task (raw bytes, no browser
cookies — the Burp-Repeater model the request tool exists for), and
relies on a path with its own undocumented footgun (eval serialization).
A real primitive is the right response; the docs note becomes part of
it (the recipe cross-references `eval` as the cookie-gated alternative
and documents the eval-serializes footgun).