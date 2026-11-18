from __future__ import annotations

import argparse
import sys

from odda import __version__


def _build_parser() -> argparse.ArgumentParser:
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
        description="Run the odda MCP server over stdio. The flags below "
        "override the session config files: the global "
        "~/.config/odda/config.toml, then the project .odda/config.toml.",
    )
    mcp_p.add_argument(
        "--max-browsers",
        type=int,
        default=None,
        metavar="N",
        help="Maximum concurrently open browsers, 0 for unlimited (default 3).",
    )
    mcp_p.add_argument(
        "--stale-browser-timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Auto-close a browser no tool call has targeted for this "
        "many seconds, 0 to disable (default 0).",
    )
    mcp_p.add_argument(
        "--vnc-bind",
        default=None,
        metavar="ADDR",
        help="IP address the per-browser noVNC endpoints bind "
        "(default 127.0.0.1). Raw VNC ports stay loopback-only.",
    )
    mcp_p.set_defaults(func=_cmd_mcp)

    init_p = sub.add_parser(
        "init-chrome-profile",
        help="One-time: open a visible Chrome to configure the base profile",
    )
    init_p.set_defaults(func=_cmd_init_chrome_profile)

    return parser


def _cmd_init_chrome_profile(_args: argparse.Namespace) -> int:
    from odda.browser import init_chrome_profile

    try:
        result = init_chrome_profile()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(
        f"Chrome: {result['chrome']}\n"
        f"Profile dir: {result['profile_dir']}\n"
        f"Status: {result['status']}"
    )
    return 0


def _cmd_mcp(args: argparse.Namespace) -> None:
    from odda.config import ConfigError, load_config
    from odda.mcp import main as mcp_main, set_config_overrides

    overrides = {
        key: value
        for key in ("max_browsers", "stale_browser_timeout", "vnc_bind")
        if (value := getattr(args, key)) is not None
    }
    try:
        load_config(overrides)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
    set_config_overrides(overrides)
    mcp_main()


def main() -> None:
    args = _build_parser().parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
