# AGENTS.md

## Project overview

`odda` is a Python CLI tool that provides browser automation and HTTP traffic capture for AI agents.

## Architecture

- `odda server` — Long-running background process holding browser, proxy, and flow-store state.
- `odda <command>` — CLI client that sends JSON-RPC requests to the server.
- OpenCode plugin — Starts the server on `session.created` and injects `ODDA_SOCKET` + `ODDA_DATA_DIR` via `shell.env`.
- Skill — Describes available CLI commands and workflows to the agent.

The proxy (mitmproxy) runs in its own thread with a dedicated asyncio event loop. Browser automation (nodriver/CDP) runs in the server's main event loop.

## Build / run

Do not use `pip` directly unless `uv` is unavailable.

```bash
# Create a Python 3.11 virtual environment with uv
uv venv --python 3.11

# Install in editable mode with dev dependencies
uv pip install -e ".[dev]"

# Install OpenCode assets
odda install-opencode

# Run tests
.venv/bin/pytest

# Lint / format
.venv/bin/ruff check .
.venv/bin/ruff format .
```

If `uv` is not available, the same commands work with a standard `python -m venv` and `pip`.


## Code conventions

- Python 3.11+ with `from __future__ import annotations`.
- Follow the existing ruff configuration.
- Keep CLI commands thin; business logic lives in the server and library modules.
- All CLI output is JSON by default.
- Errors are returned as JSON with non-zero exit codes.

## Testing

End-to-end tests live in `tests/e2e/`. They exercise the full CLI against a running server, including browser automation and proxy flow capture.

```bash
bash tests/e2e/test_all_commands.sh
```

Browser-dependent tests require Chrome.
