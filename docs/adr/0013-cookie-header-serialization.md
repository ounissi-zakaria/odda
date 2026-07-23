# Captured request/response files serialize duplicate headers per-line, not `, `-folded

odda's `_build_request_bytes` and `_build_response_headers_bytes`
(flowstore.py) serialize headers via `headers.items(multi=True)`,
emitting each duplicate header on its own CRLF-terminated line, instead
of the folding `headers.items()` which joins same-name headers with
`", "` (mitmproxy's `Headers._reduce_values`, RFC 7230 §3.2.2). No
special case is made for any header: the captured file faithfully
records what the client/server sent. Two `cookie` fragments appear as
two `cookie:` lines; two `Set-Cookie` headers appear as two
`set-cookie:` lines.

The defect the fold caused is **version-agnostic**: it fires whenever
mitmproxy's `Headers` holds more than one entry for a name, regardless
of HTTP version. Three instances, two of them common:

- **H1 response (common):** a server sending two `Set-Cookie` headers on
  the wire — legal per RFC 6265 §5.3, routine on login responses — was
  folded into `set-cookie: a=1; Path=/, b=2; Path=/`, unparseable per
  RFC 6265 §5.3 (which forbids `Set-Cookie` folding because attribute
  values like `Expires=Wed, 09 Jun 2021 ...` contain commas). Every
  multi-cookie response was silently corrupted in `response_headers`.
- **H1 request (less common):** a non-conformant H1 UA, or a hand-built
  request through the proxy, sending two `Cookie` headers was folded
  into `cookie: ..., ...` — a comma-separated cookie header some
  frameworks reject. (A conformant H1 UA sends a single `Cookie` per
  RFC 6265 §5.4, so this is rare in the wild but reachable.)
- **H2 request (per spec):** RFC 7540 §8.1.2.5 explicitly permits an
  HTTP/2 client to split cookie pairs across multiple `cookie` header
  fields, so a conformant H2 client that splits (Chrome does not, but
  odda's audience — bug-bounty hunters per ADR-0007/0012 — replays
  traffic from arbitrary H2 clients that may) hits the same fold. This
  is the cleanest reproduction and what the e2e test exercises, but it
  is one instance of the general duplicate-header defect, not the whole
  story.

We considered and rejected a `Cookie` special case that would have
re-joined multiple `cookie` fragments into one `Cookie:` line with
`"; "` (mirroring mitmproxy's own upstream normalization at
`_http1.py: request.headers["Cookie"] = "; ".join(...)`). The join
mutated the captured shape — two client-sent `cookie:` fragments became
one `Cookie:` line in the file — which contradicts REQUEST.md's
contract that the `request` file is "origin-form and goes on the wire
verbatim." A capture's job is to record, not to normalize; an agent who
wants a single joined `Cookie` for a clean H1 replay can edit the file
with `printf` (the byte-level workflow REQUEST.md already documents for
other edits). Per-line duplication is RFC 7230 §3.2.2-legal on H1, and
strictly more faithful than the `, `-folded form. If clean-H1-replay
normalization is ever wanted, it belongs as an explicit `send` flag, not
a silent mutation of every capture.

The trade-off is capture fidelity: the fold preserved mitmproxy's
in-memory surface verbatim (including its `, `-joined representation of
duplicates), while the per-line emission re-shapes duplicate headers
into the RFC-correct on-the-wire form. We chose the RFC-correct form
because the captured files exist to be read and replayed, and a faithful
copy of an unparseable header is worse than a corrected one. Header
order is preserved. `request clone` (a verbatim `shutil.copy2` of the
captured file) inherits the fix for free.

## Consequences

- Existing on-disk captures made before this change retain the old
  `, `-folded bytes; they are not rewritten. Re-capture to get the
  corrected form. This matches odda's general stance that per-flow
  files are read-only history.
- `request clone` of a pre-fix capture still carries the `, ` cookies
  — the fix is at capture time, not clone time. A future defensive
  normalization in `clone` was considered and rejected to keep `clone`
  a faithful byte copy (see REQUEST.md: "the `request` file is
  origin-form and goes on the wire verbatim").
- Captures with no duplicate headers are unaffected. Captures with
  duplicate headers — most commonly multi-`Set-Cookie` H1 responses —
  change shape, and only in the RFC-correct direction (per-line instead
  of `, `-folded).