## Agent-facing surfaces are self-contained

Agent-facing text lives in four surfaces — tool descriptions, server
instructions, tool results, error messages — and nowhere else: there are no
docs resources (`odda://docs/*` was removed; agents learn from the surfaces
themselves). All four state behavior itself and never cite repo-internal
references (ADR numbers, internal module/class/CDP API names, repo paths, CLI
flag dialect); the reader is an agent with no repo access.

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

## Architecture: thin surface, deep library

The CLI stays thin; automation logic belongs in library modules (`browser.py`,
`proxy.py`, `request/`, ...) and is exposed as MCP tools in `mcp.py` — one
`@mcp_server.tool(...)` per capability, handlers ported verbatim from the library layer.

## Tool results and errors

Tool results are text-first: `dict` returns pass through (structuredContent is the
dict itself, which omp-style clients dedupe against the text block); the
str/list/union tools pass `structured_output=False`, keep their natural
annotations, and carry no structured channel — the SDK's `{"result": ...}` wrap
would be re-appended by omp's client and double the payload. Str tools return raw
text; list/union tools wrap in `mcp.py:_json_result` (one JSON document — the
SDK's per-item list rendering loses array-ness). `screenshot` is the one
exception: its default result is a native `CallToolResult` — the path text
plus the inline JPEG image block (ADR 0027), plus a CSV legend text block
between them when `annotate=true` (ADR 0029). See ADR 0023. Anticipated errors
(`BrowserOperationError`, `ToolParamError`, `ValueError`) convert to `ToolError`
with the message verbatim via `@odda_tool`; everything else stays an SDK crash.

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