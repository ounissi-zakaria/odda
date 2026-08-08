# H2 request files gain a configurable line terminator for CRLF-in-pseudo-header attacks

`odda request send` reads an H1-shaped `request` file and, for HTTP/2, builds
H2 frames from it: the `Host` header becomes `:authority`, the request line
supplies `:method`/`:path`/`:scheme`. The parser splits the file on `\r\n`
(line terminator) and `\r\n\r\n` (block terminator = 2× line terminator). A
literal CRLF inside a header value — the payload for H2→H1 downgrade
smuggling, where a gateway translates an H2 `:path` containing `\r\n` into an
H1 request line and the CRLF splits a second request on the back-end — is
unreachable: the parser splits on it, ending the value early.

We add a per-request `line_terminator` field in `meta.json` (default `\r\n`,
set via `request new --line-terminator <bytes>`). The block terminator is
always two line terminators. The field is honored only for HTTP/2 request
files: H1 files are wire-faithful (the bytes go on the socket verbatim), so
re-framing them is either useless (bookkeeping-only) or harmful (changing
wire bytes). An agent sets the line terminator to `\n` to put a literal
`\r\n` inside an H2 header value: the parser splits on `\n` / `\n\n`, so the
CRLF in the value is preserved into the H2 frame. The agent's constraint is
that no value contains the terminator.

Header value parsing changes to support this. Today the parser strips
leading and trailing whitespace from every header value (`Name: value` →
`value`), which would eat trailing bytes the agent intends to craft. The new
rule: split each line at the first `:`, consume one optional space if
present, take the rest verbatim (name and value). `Host: example` →
`example`; `Host:  example` → ` example` (agent's leading space);
`Host: example ` → `example ` (trailing preserved); `Host:example` (no
space, legal) → `example`; `X-Empty:` → empty value. This is the HTTP
grammar (`name ":" OWS value`) with OWS = one optional space, not a strip.
Applied to all request files (H1 and H2) uniformly — the convention is the
same.

## Considered and rejected

- **Pseudo-header file format (cancelled mid-grilling).** Writing H2
  request files with `:method`/`:path`/`:scheme`/`:authority` lines instead
  of an H1-shaped request line + `Host`. More representative of H2's wire
  reality, and would have made `:authority` independently controllable from
  TCP destination (fixing the H2 vhost case that today falls back to H1.1).
  Rejected because (a) it breaks every captured H2 flow and editable request
  (hard cutover, no migration path for captures agents didn't author), (b)
  the separator does the actual smuggling work — the format change is
  cosmetic for the attack class, and (c) Burp Repeater uses the H1-shaped
  text + `Host` → `:authority` model, and odda's audience knows that model.
  The cost outweighed the benefit. The leaky abstraction (`Host` in an H2
  file is odda's input syntax for `:authority`, not a wire header) stays,
  documented.
- **Escape mechanism instead of a separator.** Keep `\r\n` as the fixed
  terminator; let the agent write `Host: /foo\r\nX-Evil: yes` with literal
  `\\r\\n` and have odda interpret the escape at send time. Rejected because
  escape characters are exactly what an agent struggles with when crafting
  binary payloads — the agent already fights shell quoting; adding a
  second escaping layer (odda's) on top is worse UX for a rare, advanced
  feature. The separator is the here-doc model (pick a delimiter that
  doesn't appear in the content), which is familiar and doesn't nest.
- **Two independent knobs (line terminator + block terminator).**
  Rejected as surface bloat. Every constructible attack (single CRLF in a
  value for header injection, double CRLF in a value for request smuggling
  through an H2→H1 gateway) is handled by one knob with block = 2× line. No
  attack requires `line=X, block≠2X`.
- **Stripping leading and trailing whitespace (status quo).** Rejected
  because trailing whitespace is a byte the agent may want (e.g. a trailing
  space in `Host` affecting vhost matching on a back-end). The single
  optional space after the colon is the convention; everything else is the
  agent's bytes.
- **Stripping nothing, requiring `Name:value` (no space).** Rejected
  because the `Name: value` convention (with a space) is universal in HTTP
  tooling and every existing request file uses it. Forcing agents to
  unlearn the convention for byte-faithfulness is a worse trade than the
  one-optional-space rule, which preserves both.

## Consequences

- `request new` gains `--line-terminator <bytes>`; `meta.json` gains
  `line_terminator` (absent = `\r\n`). No migration: existing requests and
  captures with no `line_terminator` field parse exactly as today.
- The header-value parse rule change (split on first `:`, consume one
  optional space, rest verbatim) applies to all request files. For requests
  that never relied on trailing whitespace in a header value, behavior is
  unchanged. A request that *did* rely on `.strip()` removing trailing
  whitespace (none known in tests/docs) would now send that whitespace on
  the wire — which is the point.
- The `--line-terminator` flag and the frame-source/wire-faithful
  distinction need REQUEST.md and SKILL.md updates so agents discover the
  feature and the H2-only constraint.
- The original "Document H2 `:authority` derivation from `Host` header"
  ticket remains a docs note: H2 has no `Host` header (RFC 9113); the
  `Host:` line in the request file becomes `:authority` at send time; for
  vhost/host-header labs where TCP destination and routing header must
  disagree, use HTTP/1.1. The separator feature does not change this — H2
  `:authority` is still derived from `Host`, still coupled to it.