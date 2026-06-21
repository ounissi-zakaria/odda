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
request_app = typer.Typer(name="request", help="Raw HTTP request commands")
userscript_app = typer.Typer(
    name="userscript", help="Manage userscripts that auto-run on every page"
)

app.add_typer(browser_app)
app.add_typer(tabs_app)
app.add_typer(request_app)
app.add_typer(userscript_app)


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
    js: str | None = typer.Argument(None, help="JavaScript code to execute"),
    file: Path | None = typer.Option(
        None,
        "--file",
        "-f",
        help="Read JavaScript from a file instead of the inline argument",
    ),
) -> None:
    """Execute JavaScript in the active browser tab.

    Pass JS inline as an argument, or use --file <path> to load a multi-line
    script from a file. The two are mutually exclusive.
    """
    if file is not None and js is not None:
        _output_json({"error": "Provide either inline JS or --file, not both"})
        raise typer.Exit(code=1)
    if file is None and js is None:
        _output_json({"error": "Provide inline JS or --file <path>"})
        raise typer.Exit(code=1)
    if file is not None:
        if not file.is_file():
            _output_json({"error": f"File not found: {file}"})
            raise typer.Exit(code=1)
        js = file.read_text(encoding="utf-8")
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


@request_app.command("clone")
def request_clone(
    ctx: typer.Context,
    flow_id: str = typer.Argument(..., help="Flow id to clone (e.g. 00042)"),
    name: str = typer.Option(..., "--name", help="Name for the editable request"),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing request of the same name"
    ),
) -> None:
    """Clone a captured flow's request into an editable request."""
    _run_coro(
        _client(ctx).call(
            "request/clone",
            {"flow_id": flow_id, "name": name, "force": force},
        )
    )


@request_app.command("new")
def request_new(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", help="Name for the editable request"),
    host: str = typer.Option(..., "--host", help="Target host"),
    protocol: str = typer.Option(
        "https", "--protocol", help="http or https (default https)"
    ),
    port: int | None = typer.Option(
        None, "--port", help="Target port (default 80 for http, 443 for https)"
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing request of the same name"
    ),
) -> None:
    """Create a new empty editable request."""
    _run_coro(
        _client(ctx).call(
            "request/new",
            {
                "name": name,
                "host": host,
                "protocol": protocol,
                "port": port,
                "force": force,
            },
        )
    )


@request_app.command("send")
def request_send(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Name of the editable request to send"),
    fix_content_length: bool = typer.Option(
        False,
        "--fix-content-length",
        help="Recompute Content-Length from the body before sending",
    ),
    timeout: float = typer.Option(30.0, "--timeout", help="Total timeout in seconds"),
    insecure: bool = typer.Option(
        False, "--insecure", help="Skip TLS certificate verification"
    ),
) -> None:
    """Send an editable request and record the response as a flow."""
    _run_coro(
        _client(ctx).call(
            "request/send",
            {
                "name": name,
                "fix_content_length": fix_content_length,
                "timeout": timeout,
                "insecure": insecure,
            },
        )
    )


@userscript_app.command("install")
def userscript_install(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", help="Name for the userscript"),
    file: Path | None = typer.Option(
        None, "--file", "-f", help="JavaScript file to install"
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        help="Inline JavaScript source (mutually exclusive with --file)",
    ),
) -> None:
    """Install a userscript from a file or inline source.

    The script runs at document_start in the main world on every page,
    before the page's own scripts. Overwrites any existing userscript
    of the same name.
    """
    if file is not None and source is not None:
        _output_json({"error": "Provide either --file or --source, not both"})
        raise typer.Exit(code=1)
    if file is None and source is None:
        _output_json({"error": "Provide --file <path> or --source <js>"})
        raise typer.Exit(code=1)
    if file is not None:
        if not file.is_file():
            _output_json({"error": f"File not found: {file}"})
            raise typer.Exit(code=1)
        payload: dict[str, Any] = {"name": name, "file": str(file)}
    else:
        payload = {"name": name, "source": source}
    _run_coro(_client(ctx).call("userscript/install", payload))


@userscript_app.command("list")
def userscript_list(ctx: typer.Context) -> None:
    """List installed userscripts."""
    _run_coro(_client(ctx).call("userscript/list"))


@userscript_app.command("remove")
def userscript_remove(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Name of the userscript to remove"),
) -> None:
    """Remove a userscript."""
    _run_coro(_client(ctx).call("userscript/remove", {"name": name}))


def main() -> None:
    """CLI entry point."""
    app()


if __name__ == "__main__":
    main()
