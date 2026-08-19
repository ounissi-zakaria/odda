"""odda CLI entry point."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import typer

from odda import NAVIGATE_WAIT_UNTIL_EVENTS, __version__, client, render
from odda.harness.install import Harness, install_harness

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
proxy_script_app = typer.Typer(
    name="proxy-script",
    help="Manage proxy-scripts (user-supplied mitmproxy addons that run in the proxy)",
)
coverage_app = typer.Typer(
    name="coverage", help="Block-level code coverage (start, snapshot, stop)"
)
wrap_app = typer.Typer(name="wrap", help="Function and property wraps")
wrap_calls_app = typer.Typer(name="calls", help="Install function (call) wraps")
wrap_access_app = typer.Typer(name="access", help="Install property accessor wraps")
logpoint_app = typer.Typer(
    name="logpoint", help="Non-pausing source-location observations"
)
page_app = typer.Typer(
    name="page",
    help="Page interaction: snapshot, click, fill, hover, upload (ref-driven)",
)

app.add_typer(browser_app)
app.add_typer(tabs_app)
app.add_typer(request_app)
app.add_typer(userscript_app)
app.add_typer(proxy_script_app)
app.add_typer(coverage_app)
app.add_typer(wrap_app)
wrap_app.add_typer(wrap_calls_app)
wrap_app.add_typer(wrap_access_app)
app.add_typer(logpoint_app)
app.add_typer(page_app)


def _output_json(data: Any) -> None:
    """Print JSON to stdout."""
    typer.echo(json.dumps(data, ensure_ascii=False))


# Strips the ``Server error (-NNNN): `` JSON-RPC prefix from a message
# so the text-mode error reads as ``Error: Browser 9999 not found.``
_RPC_PREFIX = re.compile(r"^Server error \(-\d+\): ")


def _emit_error(message: str, *, json_mode: bool) -> None:
    """Print an error in the requested mode.

    Text mode: ``Error: <message>`` on stderr (stdout stays clean for
    pipes). JSON mode: ``{"error": <message>}`` on stdout. The JSON-RPC
    ``Server error (-NNNN): `` prefix is stripped in text mode.
    """
    if json_mode:
        _output_json({"error": message})
        return
    clean = _RPC_PREFIX.sub("", message)
    typer.echo(f"Error: {clean}", err=True)


def _fallback_render(value: Any) -> None:
    """Print a value with no registered renderer (strings raw, else JSON)."""
    if isinstance(value, str):
        typer.echo(value)
    else:
        _output_json(value)


def _run_coro_raw(
    coro: asyncio.coroutine,  # type: ignore[type-arg]
    *,
    json_mode: bool,
    render_key: str | None = None,
) -> Any:
    """Run an async coroutine and emit its result.

    In text mode the result is passed to the renderer named by
    ``render_key`` (looked up in :data:`odda.render.RENDERERS` and
    dispatched via :func:`odda.render.render`); if no renderer is
    registered, :func:`_fallback_render` prints the raw value. In JSON
    mode the result is printed as JSON. Errors are emitted via
    :func:`_emit_error` and result in a non-zero exit code.
    """
    try:
        result = asyncio.run(coro)
    except Exception as exc:
        _emit_error(str(exc), json_mode=json_mode)
        raise typer.Exit(code=1) from exc
    if json_mode:
        _output_json(result)
        return result
    if render_key is not None and render_key in render.RENDERERS:
        typer.echo(render.render(result, render.RENDERERS[render_key]))
        return result
    _fallback_render(result)
    return result


def _run_coro(
    coro: asyncio.coroutine,  # type: ignore[type-arg]
    ctx: typer.Context,
    render_key: str,
) -> Any:
    """Run a coroutine and emit its result using the context's json flag."""
    return _run_coro_raw(coro, json_mode=ctx.obj["json"], render_key=render_key)


def _run_value(
    value: Any,
    *,
    json_mode: bool,
    render_key: str,
) -> None:
    """Emit a sync result (no coroutine) in the requested mode.

    The sync sibling of :func:`_run_coro_raw` for the few commands whose
    result is computed in-process (``version``, ``install``)
    rather than fetched from the server. Renders via the same renderer
    registry; errors go through :func:`_emit_error` by the caller.
    """
    if json_mode:
        _output_json(value)
        return
    if render_key in render.RENDERERS:
        typer.echo(render.render(value, render.RENDERERS[render_key]))
        return
    _fallback_render(value)


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
    json_mode: bool = typer.Option(
        False,
        "--json",
        help="Emit structured JSON output (default is human-readable text)",
    ),
) -> None:
    """Odda CLI."""
    ctx.obj = {"socket": socket, "data_dir": data_dir, "json": json_mode}


def _client(ctx: typer.Context) -> client.OddaClient:
    """Create a client using the socket from context or environment."""
    return client.OddaClient(socket_path=ctx.obj["socket"])


def _status_field(ctx: typer.Context, field: str, label: str, missing_msg: str) -> Any:
    """Fetch a single field from the server's ``status`` RPC.

    Args:
        ctx: Typer context (used for the client and json mode).
        field: RPC key to read (e.g. ``"data_dir"``).
        label: Human-readable name for the connection-error message
            (e.g. ``"data directory"``).
        missing_msg: Message emitted when the field is absent/empty.

    Emits an error and exits if the server can't be reached or the field is
    empty. Shared by resolvers that have no env-var shortcut.
    """
    try:
        status = asyncio.run(_client(ctx).call("status"))
    except client.OddaClientError as exc:
        _emit_error(
            f"Could not determine {label} from server: {exc}",
            json_mode=ctx.obj["json"],
        )
        raise typer.Exit(code=1) from exc
    value = status.get(field)
    if not value:
        _emit_error(missing_msg, json_mode=ctx.obj["json"])
        raise typer.Exit(code=1)
    return value


def _resolve_data_dir(ctx: typer.Context) -> Path:
    """Resolve the data directory, falling back to the server's status."""
    path = ctx.obj["data_dir"] or os.environ.get("ODDA_DATA_DIR")
    if path:
        return Path(path)
    return Path(
        _status_field(
            ctx, "data_dir", "data directory", "Server did not report a data directory"
        )
    )


def _resolve_log_path(ctx: typer.Context) -> Path:
    """Resolve the server log file path.

    Resolution order: ``ODDA_LOG`` env var (set by the plugin), then the
    server's ``status.log_path`` field. Works even when the server process
    has died, as long as ``ODDA_LOG`` is set — useful for post-mortem.
    """
    path = os.environ.get("ODDA_LOG")
    if path:
        return Path(path)
    return Path(
        _status_field(
            ctx,
            "log_path",
            "log path",
            "Server logs to stderr (no log file). Pass --log to the server "
            "or set ODDA_LOG to capture logs to a file.",
        )
    )


@app.command()
def version(ctx: typer.Context) -> None:
    """Print odda version."""
    _run_value(
        {"version": __version__}, json_mode=ctx.obj["json"], render_key="version"
    )


@app.command()
def install(ctx: typer.Context, harness: Harness) -> None:
    """Install the odda plugin and skill for a harness (opencode|pi|omp|claude)."""
    json_mode = ctx.obj["json"]
    try:
        plugin_path, skill_path = install_harness(harness)
        result = {
            "harness": harness.value,
            "plugin": str(plugin_path),
            "skill": str(skill_path),
        }
    except Exception as exc:
        _emit_error(str(exc), json_mode=json_mode)
        raise typer.Exit(code=1) from exc
    _run_value(result, json_mode=json_mode, render_key="install")


@app.command("init-chrome-profile")
def init_chrome_profile_cmd(ctx: typer.Context) -> None:
    """Launch Chrome against the base profile so you can configure it.

    Opens a visible Chrome window pointed at odda's base profile
    directory (cookies, extensions, preferences). Close the window
    when done; odda copies the configured profile into each isolated
    browser session. Fails if Chrome is not found or the base profile
    is already locked by a running Chrome.
    """
    json_mode = ctx.obj["json"]
    from odda.browser import init_chrome_profile

    try:
        result = init_chrome_profile()
    except Exception as exc:
        _emit_error(str(exc), json_mode=json_mode)
        raise typer.Exit(code=1) from exc
    _run_value(result, json_mode=json_mode, render_key="init-chrome-profile")


@app.command("server")
def server_cmd(
    socket: str = typer.Option(
        ..., "--socket", envvar="ODDA_SOCKET", help="Unix socket path to listen on"
    ),
    data_dir: str = typer.Option(
        ..., "--data-dir", envvar="ODDA_DATA_DIR", help="Data directory"
    ),
    log_path: str | None = typer.Option(
        None,
        "--log",
        help=(
            "File to append server logs to (default: stderr). "
            "Never creates the data directory."
        ),
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
    from odda import server

    server.run(
        socket_path=socket,
        data_dir=data_dir,
        parent_pid=parent_pid,
        log_path=log_path,
    )


@app.command()
def status(ctx: typer.Context) -> None:
    """Show server status."""
    _run_coro(_client(ctx).call("status"), ctx, "status")


@app.command()
def logs(
    ctx: typer.Context,
    follow: bool = typer.Option(False, "--follow", "-f", help="Tail the log file"),
    n: int = typer.Option(50, "--n", help="Number of lines to show"),
) -> None:
    """Show server logs."""
    json_mode = ctx.obj["json"]
    log_file = _resolve_log_path(ctx)
    if not log_file.exists():
        _emit_error(f"Log file not found: {log_file}", json_mode=json_mode)
        raise typer.Exit(code=1)

    if follow:
        # Tail -f: stream raw lines (text) or {"line": ...} (json).
        with log_file.open("r") as f:
            f.seek(0, 2)
            try:
                while True:
                    line = f.readline()
                    if not line:
                        time.sleep(0.1)
                        continue
                    line = line.rstrip()
                    if json_mode:
                        _output_json({"line": line})
                    else:
                        typer.echo(line)
            except KeyboardInterrupt:
                return
    else:
        lines = log_file.read_text().splitlines()[-n:]
        if json_mode:
            _output_json({"lines": lines})
        else:
            typer.echo("\n".join(lines))


@app.command()
def proxy_url(ctx: typer.Context) -> None:
    """Return the HTTP proxy URL."""
    _run_coro_raw(
        _client(ctx).call("proxy/url"),
        json_mode=ctx.obj["json"],
        render_key="proxy/url",
    )


@browser_app.command("open")
def browser_open(
    ctx: typer.Context,
    headed: bool = typer.Option(
        False, "--headed", help="Run Chrome with a visible window (default is headless)"
    ),
) -> None:
    """Open a new browser window (headless by default)."""
    _run_coro(
        _client(ctx).call("browser/open", {"headless": not headed}), ctx, "browser/open"
    )


@browser_app.command("close")
def browser_close(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Browser ID to close"),
) -> None:
    """Close a browser instance."""
    _run_coro(
        _client(ctx).call("browser/close", {"id": browser_id}), ctx, "browser/close"
    )


@browser_app.command("list")
def browser_list(ctx: typer.Context) -> None:
    """List every tracked browser with its tab count."""
    _run_coro(_client(ctx).call("browser/list", {}), ctx, "browser/list")


@app.command()
def navigate(
    ctx: typer.Context,
    url: str = typer.Option(..., "--url", help="URL to navigate to"),
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
    timeout: float = typer.Option(
        30.0, "--timeout", help="page.goto timeout in seconds (default 30)"
    ),
    wait_until: str = typer.Option(
        "load",
        "--wait-until",
        help=(
            "Playwright lifecycle event to wait for: "
            + ", ".join(NAVIGATE_WAIT_UNTIL_EVENTS)
            + " (default load)"
        ),
    ),
) -> None:
    """Navigate an existing tab to a URL.

    `--timeout` controls the ``page.goto`` timeout (default 30s,
    matching Playwright). `--wait-until` selects the lifecycle event
    navigate waits for (default ``load``). For SPAs that render at
    ``DOMContentLoaded`` but defer ``load`` (slow sub-resources), pass
    ``--wait-until domcontentloaded`` to return as soon as the DOM is
    ready instead of blocking on the 30s ``load`` timeout. On timeout,
    the error names the ``wait-until`` event that failed and points at
    ``odda wait-for`` for SPAs whose ``load`` event never fires.
    """
    _run_coro(
        _client(ctx).call(
            "navigate",
            {
                "url": url,
                "browser_id": browser_id,
                "tab_id": tab_id,
                "timeout": timeout,
                "wait_until": wait_until,
            },
        ),
        ctx,
        "navigate",
    )


@app.command("eval")
def eval_js(
    ctx: typer.Context,
    js: str | None = typer.Option(None, "--js", help="JavaScript code to execute"),
    file: Path | None = typer.Option(
        None,
        "--file",
        "-f",
        help="Read JavaScript from a file instead of the inline argument",
    ),
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
) -> None:
    """Execute JavaScript in the target tab.

    Pass JS inline as an argument, or use --file <path> to load a multi-line
    script from a file. The two are mutually exclusive.
    """
    json_mode = ctx.obj["json"]
    if file is not None and js is not None:
        _emit_error("Provide either inline JS or --file, not both", json_mode=json_mode)
        raise typer.Exit(code=1)
    if file is None and js is None:
        _emit_error("Provide inline JS or --file <path>", json_mode=json_mode)
        raise typer.Exit(code=1)
    if file is not None:
        if not file.is_file():
            _emit_error(f"File not found: {file}", json_mode=json_mode)
            raise typer.Exit(code=1)
        js = file.read_text(encoding="utf-8")
    _run_coro(
        _client(ctx).call(
            "eval",
            {"js": js, "browser_id": browser_id, "tab_id": tab_id},
        ),
        ctx,
        "eval",
    )


@app.command("wait-for")
def wait_for(
    ctx: typer.Context,
    expression: str = typer.Option(
        ..., "--expression", help="JavaScript expression to poll until truthy"
    ),
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
    timeout: float = typer.Option(
        30.0, "--timeout", help="Timeout in seconds (default 30)"
    ),
) -> None:
    """Poll a JS expression until it's truthy or timeout in the target tab.

    Uses Playwright's wait_for_function, which polls in-browser. Runs in
    the main world, so it can see page globals and userscript-injected
    helpers. Returns the truthy value on success; errors with non-zero
    exit on timeout.
    """
    _run_coro(
        _client(ctx).call(
            "wait-for",
            {
                "expression": expression,
                "timeout": timeout,
                "browser_id": browser_id,
                "tab_id": tab_id,
            },
        ),
        ctx,
        "wait-for",
    )


@app.command()
def screenshot(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
    output: str | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Path to write the JPEG to (default: a temp file)",
    ),
) -> None:
    """Capture a screenshot of the target tab's viewport."""
    payload: dict[str, Any] = {"browser_id": browser_id, "tab_id": tab_id}
    if output is not None:
        payload["output"] = output
    _run_coro(_client(ctx).call("screenshot", payload), ctx, "screenshot")


# --- page interaction (snapshot + ref-driven actions) ----------------------


@page_app.command("snapshot")
def page_snapshot(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
) -> None:
    """Take an agent-readable snapshot of the page's accessibility tree.

    Returns the a11y tree as YAML-ish text with ``[ref=eN]`` tags (or
    ``[ref=f<frameSeq>eN]`` inside iframes). Pass the ref to ``page click``,
    ``page fill``, ``page hover``, or ``page upload`` to identify the
    target. Re-snapshot to discover refs for new elements; existing refs
    continue to work until their element leaves the DOM.
    """
    _run_coro(
        _client(ctx).call(
            "page/snapshot", {"browser_id": browser_id, "tab_id": tab_id}
        ),
        ctx,
        "page/snapshot",
    )


@page_app.command("click")
def page_click(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
    ref: str = typer.Option(..., "--ref", help="Element ref from a snapshot (e.g. e2)"),
    timeout: float = typer.Option(
        5.0, "--timeout", help="Timeout in seconds (default 5)"
    ),
) -> None:
    """Click the element identified by ``ref`` (plain left-click).

    If the ref no longer resolves (element removed, navigated away),
    errors cleanly with a stale-ref message instead of hanging.
    """
    _run_coro(
        _client(ctx).call(
            "page/click",
            {
                "browser_id": browser_id,
                "tab_id": tab_id,
                "ref": ref,
                "timeout": timeout,
            },
        ),
        ctx,
        "page/click",
    )


@page_app.command("fill")
def page_fill(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
    ref: str = typer.Option(..., "--ref", help="Element ref from a snapshot"),
    value: str | None = typer.Option(
        None, "--value", help="Value to fill (clears first)"
    ),
    file: Path | None = typer.Option(
        None,
        "--file",
        "-f",
        help="Read the fill value from a file (preserves newlines)",
    ),
    timeout: float = typer.Option(
        5.0, "--timeout", help="Timeout in seconds (default 5)"
    ),
) -> None:
    """Fill the element identified by ``ref`` with ``value``.

    Clears the field first, then types the value. Works on text inputs,
    textareas, contenteditable elements, checkboxes (``"true"``/``"false"``),
    radios, and selects. Pass ``--value <string>`` inline, or ``--file <path>``
    to read the value from a file (the two are mutually exclusive); ``--file``
    avoids shell-quoting pitfalls for multiline payloads.
    """
    json_mode = ctx.obj["json"]
    if file is not None and value is not None:
        _emit_error("Provide either --value or --file, not both", json_mode=json_mode)
        raise typer.Exit(code=1)
    if file is None and value is None:
        _emit_error("Provide --value <string> or --file <path>", json_mode=json_mode)
        raise typer.Exit(code=1)
    if file is not None:
        if not file.is_file():
            _emit_error(f"File not found: {file}", json_mode=json_mode)
            raise typer.Exit(code=1)
        value = file.read_text(encoding="utf-8")
    _run_coro(
        _client(ctx).call(
            "page/fill",
            {
                "browser_id": browser_id,
                "tab_id": tab_id,
                "ref": ref,
                "value": value,
                "timeout": timeout,
            },
        ),
        ctx,
        "page/fill",
    )


@page_app.command("hover")
def page_hover(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
    ref: str = typer.Option(..., "--ref", help="Element ref from a snapshot"),
    timeout: float = typer.Option(
        5.0, "--timeout", help="Timeout in seconds (default 5)"
    ),
) -> None:
    """Hover the element identified by ``ref``.

    Auto-scrolls the element into view before hovering.
    """
    _run_coro(
        _client(ctx).call(
            "page/hover",
            {
                "browser_id": browser_id,
                "tab_id": tab_id,
                "ref": ref,
                "timeout": timeout,
            },
        ),
        ctx,
        "page/hover",
    )


@page_app.command("upload")
def page_upload(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
    ref: str = typer.Option(..., "--ref", help="Element ref of the file input"),
    files: list[str] = typer.Option(
        ...,
        "--file",
        help="Path to a file to upload (repeatable for multiple files)",
    ),
    timeout: float = typer.Option(
        5.0, "--timeout", help="Timeout in seconds (default 5)"
    ),
) -> None:
    """Upload files to a file input identified by ``ref``.

    Pass ``--file <path>`` for each file; use multiple ``--file`` flags
    for ``<input type="file" multiple>``.
    """
    _run_coro(
        _client(ctx).call(
            "page/upload",
            {
                "browser_id": browser_id,
                "tab_id": tab_id,
                "ref": ref,
                "files": files,
                "timeout": timeout,
            },
        ),
        ctx,
        "page/upload",
    )


@tabs_app.command("list")
def tabs_list(
    ctx: typer.Context,
    browser_id: int | None = typer.Option(
        None, "--browser-id", help="Filter to a specific browser"
    ),
) -> None:
    """List open tabs grouped by browser."""
    payload: dict[str, Any] = {}
    if browser_id is not None:
        payload["browser_id"] = browser_id
    _run_coro(_client(ctx).call("tabs/list", payload), ctx, "tabs/list")


@tabs_app.command("open")
def tabs_open(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    url: str | None = typer.Option(
        None, "--url", help="URL to navigate the new tab to (about:blank if omitted)"
    ),
) -> None:
    """Open a new tab in a browser.

    Without --url the new tab opens at about:blank. Returns the new tab_id.
    """
    payload: dict[str, Any] = {"browser_id": browser_id}
    if url is not None:
        payload["url"] = url
    _run_coro(_client(ctx).call("tabs/open", payload), ctx, "tabs/open")


@tabs_app.command("close")
def tabs_close(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
) -> None:
    """Close a tab in a browser."""
    _run_coro(
        _client(ctx).call("tabs/close", {"browser_id": browser_id, "tab_id": tab_id}),
        ctx,
        "tabs/close",
    )


@app.command("event-listeners")
def event_listeners(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
) -> None:
    """List JavaScript event listeners on window and document in the target tab."""
    _run_coro(
        _client(ctx).call(
            "event/listeners",
            {"browser_id": browser_id, "tab_id": tab_id},
        ),
        ctx,
        "event/listeners",
    )


@coverage_app.command("start")
def coverage_start(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
) -> None:
    """Enable precise block-level coverage on the target tab.

    Marks the tab as recording. Per-tab: starting on one tab does not
    affect another. The recording window spans navigations (per
    ADR-0005): start, navigate to trigger behavior, then snapshot or
    stop to read which code paths ran.
    """
    _run_coro(
        _client(ctx).call(
            "coverage/start",
            {"browser_id": browser_id, "tab_id": tab_id},
        ),
        ctx,
        "coverage/start",
    )


@coverage_app.command("snapshot")
def coverage_snapshot(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
) -> None:
    """Read per-script, per-block hit counts without stopping the recording.

    Zero-hit blocks are included. Script URLs are resolved via the
    server's scriptId-to-url map.
    """
    _run_coro(
        _client(ctx).call(
            "coverage/snapshot",
            {"browser_id": browser_id, "tab_id": tab_id},
        ),
        ctx,
        "coverage/snapshot",
    )


@coverage_app.command("stop")
def coverage_stop(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
) -> None:
    """Take a final coverage snapshot, stop the Profiler, and end recording.

    Returns the same per-script, per-block output as ``snapshot``; the
    recording flag is cleared as a side effect.
    """
    _run_coro(
        _client(ctx).call(
            "coverage/stop",
            {"browser_id": browser_id, "tab_id": tab_id},
        ),
        ctx,
        "coverage/stop",
    )


@wrap_calls_app.command("add")
def wrap_calls_add(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
    expr: str = typer.Option(
        ...,
        "--expr",
        help="JS expression resolving to the function to wrap (e.g. JSON.parse)",
    ),
    name: str = typer.Option(..., "--name", help="Name for the wrap"),
) -> None:
    """Install a wrap on a named function (records each call).

    The wrapper is installed as a named userscript and the extension is
    reloaded on the target browser. The wrap takes effect on the next
    navigation (re-navigate the tab or open a new one). Per ADR-0003,
    the wrap is leaf-only: it records the call it was placed on and
    does not follow callbacks passed as arguments.
    """
    _run_coro(
        _client(ctx).call(
            "wrap/calls/add",
            {
                "browser_id": browser_id,
                "tab_id": tab_id,
                "expr": expr,
                "name": name,
            },
        ),
        ctx,
        "wrap/calls/add",
    )


@wrap_access_app.command("add")
def wrap_access_add(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
    expr: str = typer.Option(
        ...,
        "--expr",
        help="Dotted JS path to the property to wrap (e.g. document.cookie)",
    ),
    name: str = typer.Option(..., "--name", help="Name for the wrap"),
) -> None:
    """Install a wrap on a property accessor (records each get/set).

    Both getter and setter are wrapped if present. A get records
    ``ret`` as the value read; a set records ``args[0]`` as the value
    written with ``ret: null``. The wrap takes effect on the next
    navigation.
    """
    _run_coro(
        _client(ctx).call(
            "wrap/access/add",
            {
                "browser_id": browser_id,
                "tab_id": tab_id,
                "expr": expr,
                "name": name,
            },
        ),
        ctx,
        "wrap/access/add",
    )


@wrap_app.command("list")
def wrap_list(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
) -> None:
    """List installed wraps.

    Wraps are stored on disk as named userscripts and apply to all
    tabs. The ``--tab-id`` is validated for targeting consistency but
    does not filter the list.
    """
    _run_coro(
        _client(ctx).call(
            "wrap/list",
            {"browser_id": browser_id, "tab_id": tab_id},
        ),
        ctx,
        "wrap/list",
    )


@wrap_app.command("remove")
def wrap_remove(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
    name: str = typer.Option(..., "--name", help="Name of the wrap to remove"),
) -> None:
    """Remove a wrap's userscript and reload the extension.

    The wrap stops recording on future navigations. Records already
    captured in the current page are not affected.
    """
    _run_coro(
        _client(ctx).call(
            "wrap/remove",
            {"browser_id": browser_id, "tab_id": tab_id, "name": name},
        ),
        ctx,
        "wrap/remove",
    )


@wrap_app.command("dump")
def wrap_dump(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
    name: str | None = typer.Option(
        None,
        "--name",
        help="Only return records from the wrap with this name",
    ),
) -> None:
    """Read the per-tab wrap record array.

    Each record is ``{wrap, type, this, args, ret, stack, error?}``.
    Functions in args/ret/this are serialized as ``{type: "function",
    name, source}`` where ``source`` is the function's ``.toString()``
    capped at 1000 chars; large or cyclic values are truncated. Records
    are wiped on navigation, so dump before navigating again. Pass
    ``--name`` to filter server-side to one wrap's records.
    """
    payload: dict[str, Any] = {"browser_id": browser_id, "tab_id": tab_id}
    if name is not None:
        payload["name"] = name
    _run_coro(_client(ctx).call("wrap/dump", payload), ctx, "wrap/dump")


@wrap_app.command("clear")
def wrap_clear(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
) -> None:
    """Zero the per-tab wrap record array without navigating.

    Wrap installations are unaffected; subsequent calls continue to
    record. Use this to reset between triggers within one page load.
    """
    _run_coro(
        _client(ctx).call(
            "wrap/clear",
            {"browser_id": browser_id, "tab_id": tab_id},
        ),
        ctx,
        "wrap/clear",
    )


@logpoint_app.command("add")
def logpoint_add(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
    url: str = typer.Option(
        ...,
        "--url",
        help="Script URL to bind the logpoint to (the script must be loaded)",
    ),
    line: int = typer.Option(
        ...,
        "--line",
        help="0-based line number in the script (minified code packs many "
        "statements per line, so the column is required to hit the right one)",
    ),
    col: int = typer.Option(
        ...,
        "--col",
        help="0-based column number (required; minified code packs many "
        "statements per line)",
    ),
    expr: str = typer.Option(
        ...,
        "--expr",
        help="JS expression to evaluate at each hit. Evaluated in the "
        "paused frame's scope, so it can read locals by name.",
    ),
) -> None:
    """Plant a non-pausing observation at a source location.

    odda plants a CDP ``Debugger.setBreakpointByUrl`` whose condition
    evaluates ``--expr`` in the paused-then-immediately-resumed
    frame's scope, records the result, and returns ``false`` so the
    page never pauses. The logpoint persists until explicitly removed
    (not fire-once); records wipe on navigation; the CDP logpoint
    re-binds to the re-loaded script. Logpoints do not survive tab
    close (per-tab-session).

    If no loaded script matches ``--url``, the command succeeds but
    the output includes a ``warning`` field. Line and column are
    0-based offsets in the script source (use the column; minified code
    packs many statements per line).
    """
    _run_coro(
        _client(ctx).call(
            "logpoint/add",
            {
                "browser_id": browser_id,
                "tab_id": tab_id,
                "url": url,
                "line": line,
                "col": col,
                "expr": expr,
            },
        ),
        ctx,
        "logpoint/add",
    )


@logpoint_app.command("list")
def logpoint_list(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
) -> None:
    """List planted logpoints for the target tab.

    Returns ``[{id, url, line, col, expr}]``.
    """
    _run_coro(
        _client(ctx).call(
            "logpoint/list",
            {"browser_id": browser_id, "tab_id": tab_id},
        ),
        ctx,
        "logpoint/list",
    )


@logpoint_app.command("dump")
def logpoint_dump(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
) -> None:
    """Read the per-tab logpoint record array.

    Each record is ``{logpoint, url, line, col, value, error}`` where
    ``error`` is ``null`` on success or the error message if the
    expression threw. Records are wiped on navigation, so dump before
    navigating again.
    """
    _run_coro(
        _client(ctx).call(
            "logpoint/dump",
            {"browser_id": browser_id, "tab_id": tab_id},
        ),
        ctx,
        "logpoint/dump",
    )


@logpoint_app.command("clear")
def logpoint_clear(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
) -> None:
    """Zero the per-tab logpoint record array without navigating.

    Logpoint installations are unaffected; subsequent hits continue to
    record. Returns ``{status: "cleared", count: <records dropped>}``.
    """
    _run_coro(
        _client(ctx).call(
            "logpoint/clear",
            {"browser_id": browser_id, "tab_id": tab_id},
        ),
        ctx,
        "logpoint/clear",
    )


@logpoint_app.command("remove")
def logpoint_remove(
    ctx: typer.Context,
    browser_id: int = typer.Option(..., "--browser-id", help="Target browser ID"),
    tab_id: int = typer.Option(..., "--tab-id", help="Target tab ID"),
    id: str = typer.Option(..., "--id", help="Logpoint id (lp-<n>) to remove"),
) -> None:
    """Remove a logpoint's CDP logpoint and registry entry.

    The logpoint stops recording on future hits. Records already
    captured in the current page are not affected.
    """
    _run_coro(
        _client(ctx).call(
            "logpoint/remove",
            {"browser_id": browser_id, "tab_id": tab_id, "id": id},
        ),
        ctx,
        "logpoint/remove",
    )


@request_app.command("clone")
def request_clone(
    ctx: typer.Context,
    flow_id: str = typer.Option(..., "--flow-id", help="Flow id to clone (e.g. 00042)"),
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
        ),
        ctx,
        "request/clone",
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
    line_terminator: str = typer.Option(
        "\r\n",
        "--line-terminator",
        help=(
            "Byte sequence the request parser splits header lines on (default "
            "CRLF, \\r\\n). H2-only: for H2->H1 downgrade smuggling where a "
            "literal CRLF must live inside an H2 header value (e.g. :path), "
            "set to \\n so the parser splits on LF, preserving CR in values. "
            "Accepts backslash escapes (\\r, \\n, \\x00, ...). Ignored for "
            "HTTP/1.1 request files (wire-faithful)."
        ),
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing request of the same name"
    ),
) -> None:
    """Create a new empty editable request."""
    lt_bytes = (
        line_terminator.encode("utf-8").decode("unicode_escape").encode("latin-1")
    )
    _run_coro(
        _client(ctx).call(
            "request/new",
            {
                "name": name,
                "host": host,
                "protocol": protocol,
                "port": port,
                "line_terminator": list(lt_bytes),
                "force": force,
            },
        ),
        ctx,
        "request/new",
    )


# Two or more --name flags selects the multi-name pipeline mode (ADR-0019);
# one --name keeps the frozen single-shot contract. --repeat >= 2 selects
# the concurrent-send mode (ADR-0020).
_PIPELINE_MIN_NAMES = 2
_REPEAT_MIN = 2


@request_app.command("send")
def request_send(
    ctx: typer.Context,
    name: list[str] = typer.Option(
        ...,
        "--name",
        help="Name of the editable request to send (repeat for multi-name pipeline)",
    ),
    repeat: int = typer.Option(
        1,
        "--repeat",
        help="Send the request N times concurrently (race / limit-overrun). "
        ">=2 enables concurrent send (H2 stream-multiplex or H1 parallel "
        "connections). Single-name only; rejected with multiple --name.",
    ),
    fix_content_length: bool = typer.Option(
        False,
        "--fix-content-length",
        help="Recompute Content-Length from the body before sending (single-name only)",
    ),
    timeout: float = typer.Option(30.0, "--timeout", help="Total timeout in seconds"),
    insecure: bool = typer.Option(
        False, "--insecure", help="Skip TLS certificate verification"
    ),
    pipelining: bool = typer.Option(
        False,
        "--pipelining",
        help="Multi-name only: send all requests then read all responses (true H1 "
        "pipelining) instead of send-then-read per request "
        "(default sequential keep-alive)",
    ),
) -> None:
    """Send an editable request and record the response as a flow.

    One ``--name`` is the single-shot contract (one flow record). Two or
    more ``--name`` flags is the multi-name pipeline: one HTTP/1.1
    connection (sequential keep-alive, or HTTP/2 concurrent
    stream-multiplex for multi-endpoint races), one flow record per
    request, a list in ``--json``. ``--repeat N`` (single ``--name``)
    sends N concurrent copies — the race / limit-overrun path. See
    ADR-0019 and ADR-0020.
    """
    is_repeat = repeat >= _REPEAT_MIN
    is_multiname = len(name) >= _PIPELINE_MIN_NAMES

    if is_repeat and is_multiname:
        _emit_error(
            "--repeat is single-name only; use --repeat with one --name, "
            "or multi-name without --repeat (the combo is ambiguous)",
            json_mode=ctx.obj["json"],
        )
        raise typer.Exit(code=1)
    if is_repeat and pipelining:
        _emit_error(
            "--repeat is concurrent; --pipelining is H1 multi-name "
            "sequential — they cannot be combined",
            json_mode=ctx.obj["json"],
        )
        raise typer.Exit(code=1)

    if is_repeat:
        payload: dict[str, Any] = {
            "name": name[0],
            "repeat": repeat,
            "fix_content_length": fix_content_length,
            "timeout": timeout,
            "insecure": insecure,
        }
    elif is_multiname:
        payload = {
            "names": name,
            "fix_content_length": fix_content_length,
            "timeout": timeout,
            "insecure": insecure,
            "pipelining": pipelining,
        }
    else:
        payload = {
            "name": name[0],
            "fix_content_length": fix_content_length,
            "timeout": timeout,
            "insecure": insecure,
        }
    _run_coro(
        _client(ctx).call("request/send", payload),
        ctx,
        "request/send",
    )


@userscript_app.command("install")
def userscript_install(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", help="Name for the userscript"),
    browser_id: int = typer.Option(
        ..., "--browser-id", help="Browser whose scope to install into"
    ),
    file: Path | None = typer.Option(
        None, "--file", "-f", help="JavaScript file to install"
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        help="Inline JavaScript source (mutually exclusive with --file)",
    ),
) -> None:
    """Install a userscript from a file or inline source into the given browser's scope.

    The script runs at document_start in the main world on every page,
    before the page's own scripts. Overwrites any existing userscript
    of the same name. The browser's extension is reloaded; existing
    already-loaded tabs are not re-injected (re-navigate to apply).
    """
    if file is not None and source is not None:
        _emit_error(
            "Provide either --file or --source, not both", json_mode=ctx.obj["json"]
        )
        raise typer.Exit(code=1)
    if file is None and source is None:
        _emit_error("Provide --file <path> or --source <js>", json_mode=ctx.obj["json"])
        raise typer.Exit(code=1)
    if file is not None:
        if not file.is_file():
            _emit_error(f"File not found: {file}", json_mode=ctx.obj["json"])
            raise typer.Exit(code=1)
        payload: dict[str, Any] = {
            "name": name,
            "browser_id": browser_id,
            "file": str(file),
        }
    else:
        payload = {"name": name, "browser_id": browser_id, "source": source}
    _run_coro(
        _client(ctx).call("userscript/install", payload),
        ctx,
        "userscript/install",
    )


@userscript_app.command("list")
def userscript_list(
    ctx: typer.Context,
    browser_id: int = typer.Option(
        ..., "--browser-id", help="Browser whose scope to list"
    ),
) -> None:
    """List installed userscripts for the given browser."""
    _run_coro(
        _client(ctx).call("userscript/list", {"browser_id": browser_id}),
        ctx,
        "userscript/list",
    )


@userscript_app.command("remove")
def userscript_remove(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", help="Name of the userscript to remove"),
    browser_id: int = typer.Option(
        ..., "--browser-id", help="Browser whose scope to remove from"
    ),
) -> None:
    """Remove a userscript from the given browser's scope and reload its extension."""
    _run_coro(
        _client(ctx).call(
            "userscript/remove", {"name": name, "browser_id": browser_id}
        ),
        ctx,
        "userscript/remove",
    )


@proxy_script_app.command("install")
def proxy_script_install(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", help="Name for the proxy-script"),
    file: Path | None = typer.Option(
        None, "--file", "-f", help="Python file (mitmproxy -s format) to install"
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        help="Inline Python source (mutually exclusive with --file)",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite an existing proxy-script of the same name",
    ),
) -> None:
    """Install a proxy-script (mitmproxy addon) from a file or inline source.

    The file is a mitmproxy ``-s`` script: its module namespace is the
    addon (top-level ``request``/``response``/``load``/``running``/...
    hooks, or an ``addons = [...]`` list). odda execs it in the running
    proxy process, so it has full server-process privileges (file and
    network access). Overwrites an existing proxy-script of the same
    name only with ``--force``. Persisted under
    ``.odda/proxy-scripts/<name>/script.py`` and re-added on server boot.
    Scope is global: one proxy shared across all browsers.
    """
    if file is not None and source is not None:
        _emit_error(
            "Provide either --file or --source, not both", json_mode=ctx.obj["json"]
        )
        raise typer.Exit(code=1)
    if file is None and source is None:
        _emit_error("Provide --file <path> or --source <py>", json_mode=ctx.obj["json"])
        raise typer.Exit(code=1)
    if file is not None:
        if not file.is_file():
            _emit_error(f"File not found: {file}", json_mode=ctx.obj["json"])
            raise typer.Exit(code=1)
        payload: dict[str, Any] = {"name": name, "file": str(file), "force": force}
    else:
        payload = {"name": name, "source": source, "force": force}
    _run_coro(
        _client(ctx).call("proxy-script/install", payload),
        ctx,
        "proxy-script/install",
    )


@proxy_script_app.command("list")
def proxy_script_list(ctx: typer.Context) -> None:
    """List installed proxy-scripts."""
    _run_coro(
        _client(ctx).call("proxy-script/list", {}),
        ctx,
        "proxy-script/list",
    )


@proxy_script_app.command("remove")
def proxy_script_remove(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", help="Name of the proxy-script to remove"),
) -> None:
    """Remove a proxy-script: delete its source and drop it from the live proxy."""
    _run_coro(
        _client(ctx).call("proxy-script/remove", {"name": name}),
        ctx,
        "proxy-script/remove",
    )


def main() -> None:
    """CLI entry point."""
    app()


if __name__ == "__main__":
    main()
