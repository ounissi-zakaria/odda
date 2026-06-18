"""odda CLI entry point."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import typer

from odda import __version__, client, server
from odda.opencode.install import install_opencode_assets

app = typer.Typer(
    name="odda",
    help="Browser automation and HTTP traffic capture CLI for AI agents",
    no_args_is_help=True,
)

browser_app = typer.Typer(name="browser", help="Browser management commands")
tabs_app = typer.Typer(name="tabs", help="Tab management commands")

app.add_typer(browser_app)
app.add_typer(tabs_app)


def _output_json(data: Any) -> None:
    """Print JSON to stdout."""
    typer.echo(json.dumps(data, ensure_ascii=False))


def _run_coro_raw(coro: asyncio.coroutine) -> Any:  # type: ignore[type-arg]
    """Run an async coroutine and return its result without printing.

    Errors are printed as JSON and result in a non-zero exit code.
    """
    try:
        return asyncio.run(coro)
    except Exception as exc:
        _output_json({"error": str(exc)})
        raise typer.Exit(code=1) from exc


def _run_coro(coro: asyncio.coroutine) -> Any:  # type: ignore[type-arg]
    """Run an async coroutine and print its result as JSON.

    Errors are printed as JSON with a non-zero exit code.
    """
    result = _run_coro_raw(coro)
    _output_json(result)
    return result


@app.callback()
def cli_callback(
    ctx: typer.Context,
    socket: str | None = typer.Option(
        None,
        "--socket",
        envvar="ODDA_SOCKET",
        help="Unix socket path of the odda server",
    ),
    data_dir: str | None = typer.Option(
        None,
        "--data-dir",
        envvar="ODDA_DATA_DIR",
        help="Data directory used by the odda server",
    ),
) -> None:
    """Odda CLI."""
    ctx.obj = {"socket": socket, "data_dir": data_dir}


def _client(ctx: typer.Context) -> client.OddaClient:
    """Create a client using the socket from context or environment."""
    return client.OddaClient(socket_path=ctx.obj["socket"])


def _resolve_data_dir(ctx: typer.Context) -> Path:
    """Resolve the data directory, falling back to the server's status."""
    path = ctx.obj["data_dir"] or os.environ.get("ODDA_DATA_DIR")
    if path:
        return Path(path)
    try:
        status = asyncio.run(_client(ctx).call("status"))
    except client.OddaClientError as exc:
        msg = f"Could not determine data directory from server: {exc}"
        raise typer.BadParameter(msg) from exc
    data_dir = status.get("data_dir")
    if not data_dir:
        msg = "Server did not report a data directory"
        raise typer.BadParameter(msg)
    return Path(data_dir)


@app.command()
def version() -> None:
    """Print odda version."""
    _output_json({"version": __version__})


@app.command()
def install_opencode() -> None:
    """Install the OpenCode plugin and skill."""
    try:
        plugin_path, skill_path = install_opencode_assets()
        _output_json(
            {
                "plugin": str(plugin_path),
                "skill": str(skill_path),
            }
        )
    except Exception as exc:
        _output_json({"error": str(exc)})
        raise typer.Exit(code=1) from exc


@app.command("server")
def server_cmd(
    socket: str = typer.Option(
        ..., "--socket", envvar="ODDA_SOCKET", help="Unix socket path to listen on"
    ),
    data_dir: str = typer.Option(
        ..., "--data-dir", envvar="ODDA_DATA_DIR", help="Data directory"
    ),
    parent_pid: int | None = typer.Option(
        None,
        "--parent-pid",
        help="Parent PID to watch for automatic shutdown",
    ),
) -> None:
    """Run the odda server.

    This command is normally started automatically by the OpenCode plugin.
    """
    server.run(socket_path=socket, data_dir=data_dir, parent_pid=parent_pid)


@app.command()
def status(ctx: typer.Context) -> None:
    """Show server status."""
    _run_coro(_client(ctx).call("status"))


@app.command()
def logs(
    ctx: typer.Context,
    follow: bool = typer.Option(False, "--follow", "-f", help="Tail the log file"),
    n: int = typer.Option(50, "--n", help="Number of lines to show"),
) -> None:
    """Show server logs."""
    log_file = _resolve_data_dir(ctx) / "server.log"
    if not log_file.exists():
        _output_json({"error": f"Log file not found: {log_file}"})
        raise typer.Exit(code=1)

    if follow:
        # Simple tail -f implementation, one JSON object per line
        with log_file.open("r") as f:
            f.seek(0, 2)
            try:
                while True:
                    line = f.readline()
                    if not line:
                        time.sleep(0.1)
                        continue
                    _output_json({"line": line.rstrip()})
            except KeyboardInterrupt:
                return
    else:
        lines = log_file.read_text().splitlines()
        _output_json({"lines": lines[-n:]})


@app.command()
def proxy_url(ctx: typer.Context) -> None:
    """Return the HTTP proxy URL."""
    result = _run_coro_raw(_client(ctx).call("proxy/url"))
    typer.echo(result)


@browser_app.command("open")
def browser_open(ctx: typer.Context) -> None:
    """Open a new browser window."""
    _run_coro(_client(ctx).call("browser/open"))


@browser_app.command("list")
def browser_list(ctx: typer.Context) -> None:
    """List open browser instances."""
    _run_coro(_client(ctx).call("browser/list"))


@browser_app.command("close")
def browser_close(
    ctx: typer.Context,
    id: int = typer.Argument(..., help="Browser ID to close"),
) -> None:
    """Close a browser instance."""
    _run_coro(_client(ctx).call("browser/close", {"id": id}))


@app.command()
def navigate(
    ctx: typer.Context,
    url: str = typer.Argument(..., help="URL to navigate to"),
    new_tab: bool = typer.Option(False, "--new-tab", help="Open in a new tab"),
) -> None:
    """Navigate the active browser to a URL."""
    _run_coro(_client(ctx).call("navigate", {"url": url, "new_tab": new_tab}))


@app.command("eval")
def eval_js(
    ctx: typer.Context,
    js: str = typer.Argument(..., help="JavaScript code to execute"),
) -> None:
    """Execute JavaScript in the active browser tab."""
    _run_coro(_client(ctx).call("eval", {"js": js}))


@app.command()
def screenshot(ctx: typer.Context) -> None:
    """Capture a screenshot of the current browser viewport."""
    _run_coro(_client(ctx).call("screenshot"))


@tabs_app.command("list")
def tabs_list(
    ctx: typer.Context,
    browser_id: int | None = typer.Option(
        None, "--browser-id", help="Filter to a specific browser"
    ),
) -> None:
    """List open tabs grouped by browser."""
    result = _run_coro_raw(_client(ctx).call("tabs/list"))
    if browser_id is not None and isinstance(result, list):
        result = [b for b in result if b.get("browser_id") == browser_id]
    _output_json(result)


@app.command()
def switch_tab(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Browser ID"),
    index: int = typer.Option(..., "--index", help="Tab index"),
) -> None:
    """Switch to a specific tab."""
    _run_coro(
        _client(ctx).call("tabs/switch", {"browser_id": browser_id, "index": index})
    )


@app.command("event-listeners")
def event_listeners(ctx: typer.Context) -> None:
    """List JavaScript event listeners on window and document."""
    _run_coro(_client(ctx).call("event/listeners"))


def main() -> None:
    """CLI entry point."""
    app()


if __name__ == "__main__":
    main()
