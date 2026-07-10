# AGENTS.md

`odda` is a Python CLI tool for browser automation and HTTP traffic capture.

- Server/client model: `odda server` runs in the background; `odda <command>` talks to it over a Unix socket via JSON-RPC.
- The OpenCode plugin starts the server and injects `ODDA_SOCKET` + `ODDA_DATA_DIR` into shell env.
- Proxy state, browser state, and captured flows all live in the server process.
- Response bodies and `flows.jsonl` are stored in `.odda/` (or the configured data dir).

## Build / run

```bash
uv venv --python 3.14
uv pip install -e ".[dev]"
odda install-opencode
```

Use `.venv/bin/python` and `.venv/bin/ruff`. Avoid `pip` directly unless `uv` is unavailable.

For a non-editable, global CLI install you can also use:

```bash
uv tool install .
```

If you add, remove, or change a dependency in `pyproject.toml`, regenerate the lockfile with `uv lock` so it stays in sync.

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
- `src/odda/browser.py` — patchright/Playwright browser automation.
- `src/odda/proxy.py` — mitmproxy wrapper.
- `src/odda/flowstore.py` — File-based flow storage (flows.jsonl + per-flow dirs).
- `src/odda/opencode/plugin.js` — OpenCode plugin.
- `src/odda/opencode/SKILL.md` — Agent skill documentation.

## Conventions

- Python 3.14+ with `from __future__ import annotations`.
- CLI commands stay thin; logic belongs in server/library modules.
- All CLI output is JSON; errors are JSON with non-zero exit codes.
- If you add, remove, or change CLI commands/options, update `src/odda/opencode/SKILL.md` and run `odda install-opencode` so agents see the current tool surface.
- When incrementing the version, update **both** `pyproject.toml` and `src/odda/__init__.py` (`__version__`), then run `uv lock` so the lockfile stays in sync. The version lives in three places: `pyproject.toml`, `src/odda/__init__.py`, and `uv.lock`.

## Agent skills

### Issue tracker

Issues are tracked as local markdown files under `.scratch/`. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles use their default names: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context repo — one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
