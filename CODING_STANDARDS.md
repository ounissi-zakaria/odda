## Agent-facing surfaces are self-contained

Tool descriptions and server instructions in `src/odda/mcp.py`, and concept docs in
`src/odda/docs/`, must state behavior itself — never cite repo-internal references
(ADR numbers, internal module/class/CDP API names, repo paths, CLI flag dialect).
The reader is an agent with no repo access.

## Architecture: thin surface, deep library

The CLI stays thin; automation logic belongs in library modules (`browser.py`,
`proxy.py`, `request/`, ...) and is exposed as MCP tools in `mcp.py` — one
`@mcp_server.tool()` per capability, handlers ported verbatim from the library layer.

## Tool results and errors

Tool results are pure natural types (`dict` passes through; `list`/`str` get the SDK's
`{"result": ...}` wrap; `Any` is text-only). Anticipated errors
(`BrowserOperationError`, `ToolParamError`, `ValueError`) convert to `ToolError` with
the message verbatim via `@odda_tool`; everything else stays an SDK crash.

## Release consistency

The version lives in three places — `pyproject.toml`, `src/odda/__init__.py`
(`__version__`), and `uv.lock` — and must move together: update both code sites, then
run `uv lock`.

## Dependencies

`patchright` stays pinned to an exact version, never ranged. A bump requires the
`chrome_args.py` drift audit — the audit protocol itself lives in AGENTS.md
(procedure, not rule).