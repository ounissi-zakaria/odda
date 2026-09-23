# AGENTS.md

`odda` is a Python package for browser automation and HTTP traffic capture, exposed as a stdio MCP server.

- MCP is the surface: `odda mcp` is the *only* odda process. The MCP server's lifespan owns the proxy, the browser manager, and the flow store; the harness spawns one process per agent session.
- The remaining CLI surface is helpers: `odda mcp` (the server) and `odda init-chrome-profile` (interactive, human-run — configures the base profile Chrome sessions are seeded from); the version probe is the `--version`/`-V` flag, not a subcommand.
- Response bodies and `flows.jsonl` are stored in `.odda/` under the MCP process's working directory.

## Build / run

```bash
uv venv --python 3.14
uv pip install -e ".[dev]"
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
pytest                          # full e2e suite (3 xdist workers — see pytest.ini)
pytest tests/e2e/test_02_userscripts.py   # one file
pytest tests/e2e -n 0           # sequential (also the mode for pdb — xdist breaks it)
```

The suite runs on the host (Chrome on `PATH` is required — the same requirement as usage). It's pytest modules under `tests/e2e/` (one module per former scrut doc, named `test_NN_<slug>.py`) driving the in-process MCP server (`Client(odda.mcp.mcp_server)`) with real Chrome, real mitmproxy, and real fixture-server subprocesses. Each test gets its own MCP session and per-test tmp data dir, and fixture ports are auto-picked, so parallel workers never collide — the request-stateless fixture servers (`fixture_site` per content, `dyn_server` per worker) are booted once and shared instead of restarted per test (see `conftest.py`). Iterate on one file first, then run the full suite to confirm nothing else broke. `tests/e2e/Dockerfile` still exists as the seed image for a future CI runner but is not part of the documented workflow.

**When running tests, read the full output.** Do not pipe test commands through `grep`, `head`, `tail`, or any truncation. Grep for a pass/fail marker and you will miss the failure context (the diff block, the stderr traceback, which doc actually failed) and end up re-running the suite to recover what the first run already told you. The Bash tool captures the full output to a file when it exceeds the display window — read that file with the Read tool (offset/limit) instead of truncating on the shell side.

## Conventions

Code standards live in `CODING_STANDARDS.md` (thin-CLI architecture, tool
results/errors, release consistency, self-contained agent-facing surfaces). The
patchright pin's drift-audit *procedure* stays here: on a bump, audit
`src/odda/chrome_args.py` against the new `chromiumSwitches` block in the installed
driver's `coreBundle.js` (search `init_chromiumSwitches`): diff `_DISABLED_FEATURES`
against the driver's `disabledFeatures` array and `_CHROMIUM_SWITCHES` against the
driver's desktop `chromiumSwitches()` resolution (the driver's `_innerDefaultArgs`
calls it with no options — the `android: true` branch that drops `--disable-sync`
only fires on the Android path, not `launch_persistent_context`). Confirm two
deliberate exclusions stay absent: `--password-store=basic` and
`--use-mock-keychain` (real system keychain so seeded cookies persist). Confirm
`--disable-blink-features=AutomationControlled` (stealth) stays present — no e2e doc
checks `navigator.webdriver`, so the pin + this note are the only guards against a
silent stealth regression.

## Agent skills

### Issue tracker

Issues are tracked as local markdown files under `.scratch/`. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles use their default names: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context repo — one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
