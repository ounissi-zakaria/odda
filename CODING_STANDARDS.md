## Agent-facing surfaces are self-contained

Agent-facing text lives in four surfaces — tool descriptions, server
instructions, tool results, error messages — plus the one docs-resource
exception below (`odda://docs/*` was otherwise removed; agents learn from
the surfaces themselves). All surfaces state behavior itself and never
cite repo-internal references (ADR numbers, internal module/class/CDP API
names, repo paths, CLI flag dialect); the reader is an agent with no
repo access.

- Tool descriptions are one line: the name-level contract. Only contracts an
  agent cannot infer and would silently get wrong get more (request-file
  wire-faithful vs frame-source, wrap/logpoint effect timing).
- Constraints live in the error that fires, with the fix — not in the
  description. Describe what exists, never what to do: no "prefer X", no
  workflows, no usage recipes.
- Server instructions carry the session mental model only (capture invariant,
  `flows.jsonl` index, body decode semantics), phrased as what exists.
- Results teach their own shape: legends, sentinels, and cap notes ride on
  the result and are documented nowhere else.

**The docs-resource exception** (ADR 0038): a docs resource may exist
only for content no tool surface can carry and an agent cannot proceed
without. The rule guide (`odda://scanner/rule-guide`) is the standing
instance — the rule-file contract, the FlowView API, and worked examples
of both rule styles cannot ride a one-line tool description, install
errors can only repeat the file shape at the point of failure, and no
rule can be authored without them. Everything that fails this test goes
to the four surfaces, where it belongs.

## Architecture: thin surface, deep library

The CLI stays thin; automation logic belongs in library modules (`browser.py`,
`proxy.py`, `request/`, ...) and is exposed as MCP tools in `mcp.py` — one
`@mcp_server.tool(...)` per capability, handlers ported verbatim from the library layer.

## Tool results and errors

Tool results are natural: every tool returns its declared type and the SDK
derives the structured channel from the annotation — `dict` returns pass
through as themselves, str/list/union values arrive wrapped as
`{"result": ...}`. The harness delivers the payload once by deduping the
structured channel against the text blocks; omp ≥18.20.0 does (raw-string,
`{"result"}`-unwrap, and per-element list matching — binary-verified
2026-11-13), so no server-side workaround exists anymore — the
`structured_output=False`/`_json_result`
flattening is gone. `screenshot` is the one exception: it still passes
`structured_output=False` and its default result is a native
`CallToolResult` — the path text plus the inline JPEG image block
(ADR 0027), plus a CSV legend text block between them when `annotate=true`
(ADR 0029). Anticipated errors (`BrowserOperationError`, `ToolParamError`,
`ValueError`) convert to `ToolError` with the message verbatim via
`@odda_tool`; everything else stays an SDK crash.

## Release consistency

The version lives in three places — `pyproject.toml`, `src/odda/__init__.py`
(`__version__`), and `uv.lock` — and must move together: update both code sites, then
run `uv lock`.

Version bumps happen only on explicit request: the user merges dev branches first and
increments the version afterwards, so never touch the version on a dev branch unless
asked.

## Dependencies

`patchright` stays pinned to an exact version, never ranged. A bump requires the
`chrome_args.py` drift audit — the audit protocol itself lives in AGENTS.md
(procedure, not rule).