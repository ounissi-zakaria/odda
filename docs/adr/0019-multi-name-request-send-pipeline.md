# `request send` gains a multi-name pipeline mode (arity-driven), not a new `pipeline` command

`odda request send --name <a> [--name <b> ...]` accepts repeated `--name` flags.
One `--name` is today's single-shot send (contract frozen bit-for-bit); two or
more `--name` is a **pipeline** — one H1 connection, requests sent and responses
read in order, connection kept open across sends and closed at the end. This
enables the same-connection attack class (response-queue poisoning, same-
connection CL.0 confirmation) that repeated single-name sends cannot, because
those open a fresh connection each time and the poisoning/poisoned-response
does not survive across connections.

A new `request pipeline` command was considered and rejected in favor of
extending `send`. The trade-off: a separate command keeps `send`'s output
contract literally frozen and isolates the multi-send loop's error policy in
its own code path, but multi-name `send` is more discoverable — one command
that "does more when you give it more", the named-args self-documenting
property that survives in `request_send`'s named params. The contract-stability
cost is paid additively, not as a break: single-name output is frozen; the
multi-name list shape is a new output mode opted into by passing a second
`--name` (which no existing consumer does).

## Behavior

- **Single-name (frozen):** one `--name` → one flow record, today's `key:
  value` text and `--json` object, connection closed after the response.
  Unchanged.
- **Multi-name (new):** two+ `--name` → N flow records. `--json` returns a
  list `[{...}, ...]`; text renders one block per request (today's per-flow
  block) separated by a blank line. Every response has a corresponding send,
  so the flow model stays the uniform one-request-one-response shape — no
  request-less responses, no flow-format extension, no "phantom smuggled
  request" extraction. Each `--name` is an explicitly-authored editable
  request; the agent authors the victim request as its own file.

## `--pipelining` flag (the one behavioral flag accepted)

Default multi-name mode is **sequential keep-alive**: send-then-read per
request, repeated. This covers response-queue poisoning (send smuggling →
read resp1 → send victim → read resp2 = the smuggled response delivered to
the victim) and same-connection CL.0 confirmation.

`--pipelining` switches to **send-all-then-read-all** (true H1 pipelining):
write every request's bytes up front, then read every response. This covers
victim-consumption, where the victim request must arrive while the server is
still parsing the smuggling POST's "body" so the victim gets consumed as
body bytes; send-then-read lets the server finish the smuggling POST first
and the attack never fires.

This flag is accepted despite the general principle (below) that behavioral
modes are command/arity choices, not flags, because the *mechanism* genuinely
branches on the attack variant and cannot be inferred from arity. A future
contributor reading this should not treat `--pipelining` as precedent for a
`--keep-alive` flag (rejected below) — arity-driven behavior is a command
choice; mechanism-driven behavior is a flag. The distinction is which kind
of decision the agent is making.

## Rejected flags and shapes

- **No `--keep-alive` flag.** The command/arity choice *is* the keep-alive
  behavior: single-name = one-shot, connection closed; multi-name =
  connection kept open across sends. A `--keep-alive` flag on `send` is
  vestigial — there's no second send for it to enable. The doc proposed it;
  rejected because the behavior is already expressed by passing a second
  `--name`.
- **No `--read-trailing` / passive multi-response read.** The doc proposed
  reading N responses after a single send to catch servers that push a
  second response unsolicited. Rejected because sequential keep-alive
  subsumes it: the unsolicited-pushed response is "the next response on the
  wire," consumed by the next `send`'s read. Keeping `--read-trailing`
  would produce request-less responses (a response with no corresponding
  send), breaking the uniform one-request-one-response flow model and
  requiring a flow-format extension or synthetic empty request files. Drop
  it; revisit only if a concrete attack needs request-less-response capture.
- **No `--fix-content-length` with multi-name.** `--fix-content-length`
  recomputes `Content-Length` from the body, which would overwrite the
  intentionally-wrong `Content-Length` that smuggling payloads depend on
  (CL.0, CL off-by-one). Applying it to a pipeline silently destroys the
  attack. Multi-name + `--fix-content-length` errors pre-emptively. The
  agent who wants CL-fixed requests sends each single-name with the flag.
- **No H2 in multi-name.** If any `--name`'s request line says `HTTP/2`,
  multi-name errors pre-emptively: H1-style smuggling is meaningless in
  pure H2 (binary framing removes the CL/TE ambiguity). Cross-protocol H2
  smuggling is a different attack class; the agent uses single-name `send`
  for any H2 work. A parallel `send_h2_pipeline` via stream multiplexing was
  considered and rejected as scope creep for a non-use-case.
- **No bare-body requests in multi-name.** A request with a body but no
  `Content-Length` and no `Transfer-Encoding` triggers `write_eof` (half-
  close) in `send_h1` so the server sees EOF as the body terminator. EOF
  ends the connection, so req2 cannot follow. Multi-name errors pre-
  emptively if any `--name` has this shape. Every documented smuggling
  variant (CL.0, CL.TE, TE.CL, TE.TE, victim-consumption) uses at least one
  framing header, so nothing legitimate is blocked. The agent's client
  stays a clean, deterministic H1 sender; the *server's* parsing quirks are
  what's under test, not the client's.

## Durability, errors, timeout

- **Pre-write all N.** Before opening the socket, allocate N flow ids and
  write all N request files + `meta.json` sidecars. Then send. On crash
  mid-pipeline, all N request files are durable; response files exist only
  for responses actually received. Matches today's two-phase durability
  ("request recorded before the network exchange, so a crash leaves a
  durable request file") scaled to N.
- **Error policy.** If step N fails (timeout, connection drop): step N
  gets today's error flow (`error` file + `flows.jsonl` line with
  `status_code: null`); steps N+1..end each get an error flow with an
  "aborted: step N failed" message (their pre-written request files
  already exist); the connection closes unconditionally (the socket state
  is untrusted after a mid-sequence read failure). `flows.jsonl` stays
  complete — every pre-written request has a corresponding line (response,
  network error, or abort error). No "continue on a fresh connection"
  behavior: the smuggling attack may have left the server in a weird
  state, a fresh connection loses the response-queue-poisoning context,
  and "continue after error" is a behavior nobody asked for.
- **One `--timeout` for the whole pipeline.** The documented "Total
  timeout for connect + reads" semantics extend to cover connect + all
  sends + all reads for the whole sequence. The agent picks a generous
  total for slow smuggling responses. A `--total-timeout` / per-step split
  was rejected as surface bloat with no documented attack needing both
  knobs; the per-step concern (one step eating the budget) is managed by
  picking a generous total, not by per-step knobs.