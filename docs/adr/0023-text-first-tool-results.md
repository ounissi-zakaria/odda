# Text-first tool results: dict pass-through, everything else text-only

`mcp==2.1.1` builds two channels for a tool result: the text `content`
blocks and (for `str`/`list`/generic returns) a `structuredContent` of
`{"result": <value>}`. The omp harness appends that structured channel to
the model's context as a second text block whenever no content block
deep-equals it — which is *never* true for wrapped `str`/`list` values
(their text channel is the bare value, not the dict). So every
`page_snapshot` — the largest payload in the surface, ~50 KB on a
content-heavy page — reached the model twice, and nine list tools plus
`request_send`'s union duplicated the same way (issue
`#15-structured-content-duplication`).

Decision (amending the "pure natural types" contract of ticket #03,
decision (c)): **the wire carries the payload exactly once.**

- `dict[str, Any]` returns keep the natural pass-through: structured
  content is the dict itself, and omp's dedupe suppresses it because the
  text channel is the dict's own indented JSON. Other MCP clients keep
  genuine structured data where the value actually is an object.
- `str` returns annotate `-> Any`: text-only, and the text is the raw
  string (`page_snapshot`'s tree, `screenshot`'s path, `proxy_url`).
- `list`/union returns annotate `-> Any` *and* return an explicit
  `CallToolResult` via `_json_result()` (`mcp.py`): one indented-JSON
  document, no structured channel. Bare `-> Any` list returns were
  measured to render per-item — a 1-item list loses its array-ness and
  an empty list emits no block at all — and the SDK passes an explicit
  `CallToolResult` through verbatim.

Rejected: fixing only `page_snapshot` (leaves the contract half-natural
for the rest); fixing omp's dedupe upstream (the general cure, but not
this repo — declined for now); leaving the duplication (2× context per
snapshot in the primary harness).

## Consequences

- The 13 flattened tools (`page_snapshot`, `screenshot`, `proxy_url`,
  `browser_list`, `tabs_list`, `event_listeners`, `wrap_list`,
  `wrap_dump`, `logpoint_list`, `logpoint_dump`, `userscript_list`,
  `proxy_script_list`, `request_send`) advertise no `outputSchema` — the
  lost schemas only ever described the `{"result": ...}` wrap.
- A programmatic (non-LLM) MCP client loses the structured channel on
  those 13 tools; dict tools keep it.
- The e2e harness reads these tools' text channel (`Harness.call_json`,
  `tests/e2e/conftest.py`); `Harness.call` no longer unwraps
  `{"result": ...}` — that wrap no longer exists anywhere.
- No regression pin was added for the flattened contract (deliberate,
  per the #15 grilling) — with one side effect of the forced test
  repair: test_08's rewritten request_send assertion does pin that
  tool's wire shape (`structured_content is None`, single JSON text
  block). The other 12 tools are unpinned: a revert of their
  annotations back to natural types passes the whole suite while
  silently reintroducing the 2× echo in omp. This ADR, the `mcp.py`
  module docstring, and `CODING_STANDARDS.md` are the guards.
