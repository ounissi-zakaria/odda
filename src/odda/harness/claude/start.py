#!/usr/bin/env python3
"""odda SessionStart hook for Claude Code.

Idempotently starts the per-session odda server (it outlives this short-lived
hook process via reparenting — not detached into a new session — and its
lifetime is bound to the Claude Code process via --parent-pid) and exports
``ODDA_SOCKET`` / ``ODDA_DATA_DIR`` / ``ODDA_LOG`` into ``$CLAUDE_ENV_FILE``,
which Claude Code sources as a preamble before every subsequent Bash command.

Re-runs (clear / compact / resume re-fire SessionStart) skip the start when a
server is already listening on the socket, and re-append the env exports (same
values, harmless). odda's CLI client waits for the socket to appear on first
use, so the server may still be booting when this hook returns.

Written in Python rather than shell so the env-file values are quoted with
``shlex.quote`` (stdlib) and the socket liveness probe is a native ``AF_UNIX``
connect — no hand-rolled quoting or inline-Python heredoc. The hook process's
runtime is the same ``python3`` odda already requires.
"""

from __future__ import annotations

import os
import shlex
import signal
import socket
import subprocess
from pathlib import Path


def _socket_alive(path: str) -> bool:
    """True if a server is currently listening on the given Unix socket."""
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(0.25)
    try:
        sock.connect(path)
    except OSError:
        return False
    finally:
        sock.close()
    return True


def main() -> None:
    """Start the per-session odda server (if not already up) and export its env."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/tmp")  # noqa: S108
    # Claude Code exports its own PID as CLAUDE_PID. os.getppid() is a fallback
    # for environments that don't set it; in the normal case it equals CLAUDE_PID
    # anyway (the hook process's parent is the Claude Code process).
    claude_pid = os.environ.get("CLAUDE_PID") or str(os.getppid())
    socket_path = f"{runtime_dir}/odda-{claude_pid}.sock"
    log_path = f"{runtime_dir}/odda-{claude_pid}.log"
    data_dir = f"{os.environ.get('CLAUDE_PROJECT_DIR') or Path.cwd()}/.odda"
    odda_bin = os.environ.get("ODDA_BIN", "odda")

    if not _socket_alive(socket_path):
        # No live server. A stale socket file (crashed server) is safe: odda
        # server unlinks and rebinds on startup. The server must outlive this
        # short-lived hook process but is NOT detached into a new session
        # (matches opencode's ``detached: false``): a child reparents to init
        # when its parent exits, so it keeps running after the hook returns
        # without blocking it. Ignore SIGHUP here so the inherited disposition
        # (preserved across exec) protects the server from a SIGHUP to the
        # hook's process group when the hook exits — the nohup equivalent,
        # without setsid. --parent-pid makes odda self-terminate when the Claude
        # Code process exits, so no SessionEnd hook is needed.
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        with Path(log_path).open("ab") as log:
            subprocess.Popen(  # noqa: S603  # trusted local binary, user-controlled ODDA_BIN
                [
                    odda_bin,
                    "server",
                    "--socket",
                    socket_path,
                    "--data-dir",
                    data_dir,
                    "--log",
                    log_path,
                    "--parent-pid",
                    claude_pid,
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )

    # Export env for every subsequent Bash command. Idempotent: same values
    # each time; re-appending on clear/compact/resume keeps the env populated if
    # Claude Code re-initialises $CLAUDE_ENV_FILE.
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if env_file:
        with Path(env_file).open("a", encoding="utf-8") as f:
            f.writelines(
                f"export {key}={shlex.quote(value)}\n"
                for key, value in (
                    ("ODDA_SOCKET", socket_path),
                    ("ODDA_DATA_DIR", data_dir),
                    ("ODDA_LOG", log_path),
                )
            )


if __name__ == "__main__":
    main()
