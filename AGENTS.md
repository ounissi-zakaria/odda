# AGENTS.md

`odda` is a Python CLI tool for browser automation and HTTP traffic capture.

- Server/client model: `odda server` runs in the background; `odda <command>` talks to it over a Unix socket via JSON-RPC.
- The OpenCode plugin starts the server and injects `ODDA_SOCKET` + `ODDA_DATA_DIR` into shell env.
- Proxy state, browser state, and captured flows all live in the server process.
- Response bodies and `flows.db` are stored in `.odda/` (or the configured data dir).

## Build / run

```bash
uv venv --python 3.11
uv pip install -e ".[dev]"
odda install-opencode
```

Use `.venv/bin/python` and `.venv/bin/ruff`. Avoid `pip` directly unless `uv` is unavailable.

## Useful commands

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
bash tests/e2e/test_all_commands.sh
```

E2E tests require Chrome.

## Key files

- `src/odda/cli.py` — Typer CLI commands.
- `src/odda/server.py` — JSON-RPC server and request handlers.
- `src/odda/client.py` — JSON-RPC client.
- `src/odda/browser.py` — nodriver/CDP browser automation.
- `src/odda/proxy.py` — mitmproxy wrapper.
- `src/odda/database.py` — SQLite flow storage and queries.
- `src/odda/opencode/plugin.js` — OpenCode plugin.
- `src/odda/opencode/SKILL.md` — Agent skill documentation.

## Conventions

- Python 3.11+ with `from __future__ import annotations`.
- CLI commands stay thin; logic belongs in server/library modules.
- All CLI output is JSON; errors are JSON with non-zero exit codes.
- If you add, remove, or change CLI commands/options, update `src/odda/opencode/SKILL.md` and run `odda install-opencode` so agents see the current tool surface.
