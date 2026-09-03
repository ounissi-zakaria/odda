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
./scripts/test-e2e.sh 02            # one file (prefix, bare name, or full path)
./scripts/test-e2e.sh 02 05 07      # a subset
```

E2E tests run inside a Docker container (built from `tests/e2e/Dockerfile`) that carries Chrome and all system deps; no host Chrome required. The suite is pytest modules under `tests/e2e/` (one module per former scrut doc, named `test_NN_<slug>.py`) driving the in-process MCP server (`Client(odda.mcp.mcp_server)`) with real Chrome, real mitmproxy, and real fixture-server subprocesses; a thin stdio leg (`test_99_stdio.py`) pins the subprocess wire. `-j N` maps to pytest-xdist workers (default 4). Each test gets its own MCP session, per-test tmp data dir, and auto-picked fixture ports, so parallel workers never collide. Positional args select specific test files (number prefix like `02`, bare filename, or full path); with no args the full suite runs. Iterate on one file first, then run the full suite to confirm nothing else broke. On the host, `pytest tests/e2e -n 4` works directly if Chrome and `uv pip install -e ".[dev]"` are present.

**When running tests, read the full output.** Do not pipe test commands through `grep`, `head`, `tail`, or any truncation. Grep for a pass/fail marker and you will miss the failure context (the diff block, the stderr traceback, which doc actually failed) and end up re-running the suite to recover what the first run already told you. The Bash tool captures the full output to a file when it exceeds the display window — read that file with the Read tool (offset/limit) instead of truncating on the shell side.

## Key files

- `src/odda/cli.py` — Typer CLI commands.
- `src/odda/server.py` — JSON-RPC server and request handlers.
- `src/odda/client.py` — JSON-RPC client.
- `src/odda/browser.py` — patchright/Playwright browser automation.
- `src/odda/chrome_args.py` — Redeclared Chrome launch flags (the patchright `chromiumSwitches` mirror + m150 model-store suppression); see the drift audit note below.
- `src/odda/proxy.py` — mitmproxy wrapper.
- `src/odda/flowstore.py` — File-based flow storage (flows.jsonl + per-flow dirs).
- `src/odda/harness/opencode/plugin.js` — OpenCode plugin.
- `src/odda/harness/pi/plugin-pi.ts` — pi extension (process.env inheritance).
- `src/odda/harness/omp/plugin-omp.ts` — omp extension (per-call `tool_call` env injection).
- `src/odda/harness/claude/start.py` — Claude Code `SessionStart` hook (starts the odda server, outliving the hook via reparenting — not detached into a new session — and bound to `$CLAUDE_PID` via `--parent-pid`; writes `ODDA_SOCKET`/`ODDA_DATA_DIR`/`ODDA_LOG` into `$CLAUDE_ENV_FILE`); shipped as `scripts/start.py` inside the plugin bundle that `odda install claude` writes to `~/.claude/skills/odda/`.
- `src/odda/harness/install.py` — Harness install dispatcher (`odda install <opencode|pi|omp|claude>`).
- `src/odda/harness/skill/SKILL.md` — Agent skill documentation (shared across harnesses).

## Conventions

- Python 3.14+ with `from __future__ import annotations`.
- CLI commands stay thin; logic belongs in server/library modules.
- CLI output defaults to human-readable text; the global `--json` flag opts into structured output (stable by convention; the parse target for scripts). Errors print as `Error: <message>` on stderr with a non-zero exit code in text mode (`{"error": ...}` on stdout in `--json` mode).
- If you add, remove, or change CLI commands/options, update `src/odda/harness/skill/SKILL.md` and run `odda install opencode` so agents see the current tool surface. Text renderers live in `src/odda/render.py` — add one for any new command whose result a human or agent will read.
- Skill docs (`SKILL.md` and linked `*.md`) describe behavior, not implementation — keep ADR refs, internal class/module/library/CDP API names, exact on-disk modes, and other internals out of them.
- When incrementing the version, update **both** `pyproject.toml` and `src/odda/__init__.py` (`__version__`), then run `uv lock` so the lockfile stays in sync. The version lives in three places: `pyproject.toml`, `src/odda/__init__.py`, and `uv.lock`.
- **`patchright` is pinned to an exact version** (`patchright==<x.y.z>` in `pyproject.toml`). On a bump, audit `src/odda/chrome_args.py` against the new `chromiumSwitches` block in the installed driver's `coreBundle.js` (search `init_chromiumSwitches`): diff `_DISABLED_FEATURES` against the driver's `disabledFeatures` array and `_CHROMIUM_SWITCHES` against the driver's desktop `chromiumSwitches()` resolution (the driver's `_innerDefaultArgs` calls it with no options — the `android: true` branch that drops `--disable-sync` only fires on the Android path, not `launch_persistent_context`). Confirm two deliberate exclusions stay absent: `--password-store=basic` and `--use-mock-keychain` (real system keychain so seeded cookies persist). Confirm `--disable-blink-features=AutomationControlled` (stealth) stays present — no e2e doc checks `navigator.webdriver`, so the pin + this note are the only guards against a silent stealth regression.

## Agent skills

### Issue tracker

Issues are tracked as local markdown files under `.scratch/`. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles use their default names: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context repo — one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
