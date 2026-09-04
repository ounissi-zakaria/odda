"""odda CLI entry point.

Two commands survive the MCP migration (map decision: "CLI survivors")
plus a ``--version`` flag:

- ``mcp`` — the stdio MCP server; the *only* odda process. Spawning it is
  the harness's job (see README for per-harness config).
- ``init-chrome-profile`` — one-time, human-run, opens a visible Chrome
  window; cannot be an MCP tool because it blocks on the user closing
  the window (map decision: init-chrome-profile stays CLI-only).
- ``--version`` / ``-V`` — version probe for scripts and bug reports
  (the old ``version`` subcommand moved here; lowercase ``-v`` stays free
  for a future verbose flag).

Everything odda used to do over the JSON-RPC CLI lives as MCP tools in
:mod:`odda.mcp` — one tool per former command, driven by the agent's
MCP client.
"""

from __future__ import annotations

import argparse
import sys

from odda import __version__


def _build_parser() -> argparse.ArgumentParser:
    """Build the two-command argument parser."""
    parser = argparse.ArgumentParser(
        prog="odda",
        description="Browser automation and HTTP traffic capture for AI agents "
        "(MCP server + one helper command).",
        epilog="All automation, capture, and analysis lives behind the MCP "
        "tools exposed by 'odda mcp' — see the README for harness config.",
    )
    parser.add_argument(
        "--version", "-V", action="version", version=f"odda {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    mcp_p = sub.add_parser(
        "mcp",
        help="Run the odda MCP server over stdio (spawned by a harness)",
    )
    mcp_p.set_defaults(func=_cmd_mcp)

    init_p = sub.add_parser(
        "init-chrome-profile",
        help="One-time: open a visible Chrome to configure the base profile",
    )
    init_p.set_defaults(func=_cmd_init_chrome_profile)

    return parser


def _cmd_init_chrome_profile(_args: argparse.Namespace) -> int:
    """Launch Chrome against the base profile so the user can configure it.

    Opens a visible Chrome window pointed at odda's base profile
    directory (cookies, extensions, preferences). Close the window
    when done; odda copies the configured profile into each isolated
    browser session. Fails if Chrome is not found or the base profile
    is already locked by a running Chrome.
    """
    from odda.browser import init_chrome_profile

    try:
        result = init_chrome_profile()
    except Exception as exc:  # single error surface for a human
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(
        f"Chrome: {result['chrome']}\n"
        f"Profile dir: {result['profile_dir']}\n"
        f"Status: {result['status']}"
    )
    return 0


def _cmd_mcp(_args: argparse.Namespace) -> None:
    """Run the stdio MCP server; blocks until the client disconnects."""
    # Deferred import: dragging the mcp SDK (and its mitmproxy/browser
    # imports) into every `odda --version`/`--help` run costs ~1.3s; the
    # mcp path pays it alone (lazy-CLI-imports convention, commit a0a2f5c).
    from odda.mcp import main as mcp_main

    mcp_main()


def main() -> None:
    """CLI entry point."""
    args = _build_parser().parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
