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
odda install opencode
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
./scripts/test-e2e.sh -j4
```

E2E tests run inside a Docker container (built from `tests/e2e/Dockerfile`) that carries Chrome, scrut, and all system deps. No host Chrome or scrut installation required. `-j N` controls parallelism across test documents (default 1, use `-j4` for speed, `-j0` for unlimited). Each `scrut test <file>` runs in its own `$PWD` with its own odda server and auto-picked fixture port, so parallel docs never collide.

**When running tests, read the full output.** Do not pipe test commands through `grep`, `head`, `tail`, or any truncation. Grep for a pass/fail marker and you will miss the failure context (the diff block, the stderr traceback, which doc actually failed) and end up re-running the suite to recover what the first run already told you. The Bash tool captures the full output to a file when it exceeds the display window — read that file with the Read tool (offset/limit) instead of truncating on the shell side.

## Key files

- `src/odda/cli.py` — Typer CLI commands.
- `src/odda/server.py` — JSON-RPC server and request handlers.
- `src/odda/client.py` — JSON-RPC client.
- `src/odda/browser.py` — patchright/Playwright browser automation.
- `src/odda/proxy.py` — mitmproxy wrapper.
- `src/odda/flowstore.py` — File-based flow storage (flows.jsonl + per-flow dirs).
- `src/odda/harness/opencode/plugin.js` — OpenCode plugin.
- `src/odda/harness/pi/plugin-pi.ts` — pi + omp extension (process.env inheritance; one source, two install targets).
- `src/odda/harness/install.py` — Harness install dispatcher (`odda install <opencode|pi|omp>`).
- `src/odda/harness/skill/SKILL.md` — Agent skill documentation (shared across harnesses).

## Conventions

- Python 3.14+ with `from __future__ import annotations`.
- CLI commands stay thin; logic belongs in server/library modules.
- CLI output defaults to human-readable text; the global `--json` flag opts into structured output (stable by convention; the parse target for scripts). Errors print as `Error: <message>` on stderr with a non-zero exit code in text mode (`{"error": ...}` on stdout in `--json` mode).
- If you add, remove, or change CLI commands/options, update `src/odda/harness/skill/SKILL.md` and run `odda install opencode` so agents see the current tool surface. Text renderers live in `src/odda/render.py` — add one for any new command whose result a human or agent will read.
- Skill docs (`SKILL.md` and linked `*.md`) describe behavior, not implementation — keep ADR refs, internal class/module/library/CDP API names, exact on-disk modes, and other internals out of them.
- When incrementing the version, update **both** `pyproject.toml` and `src/odda/__init__.py` (`__version__`), then run `uv lock` so the lockfile stays in sync. The version lives in three places: `pyproject.toml`, `src/odda/__init__.py`, and `uv.lock`.

## Agent skills

### Issue tracker

Issues are tracked as local markdown files under `.scratch/`. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles use their default names: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context repo — one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
