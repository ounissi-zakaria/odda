# See ADR-0027.
from __future__ import annotations

import asyncio
import base64
import functools
import re
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context  # noqa: TC002
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent

from odda import NAVIGATE_WAIT_UNTIL_EVENTS, __version__, flowstore, proxyscript
from odda.browser import BrowserManager, BrowserOperationError, warm_driver
from odda.config import CONFIG_FILE_NAME, load_config
from odda.paths import global_rules_dir, project_data_dir
from odda.proxy import ProxyServer
from odda.request import (
    clone as clone_request,
    new as new_request,
    send as send_request,
    send_pipeline as send_request_pipeline,
    send_repeat as send_request_repeat,
)
from odda.scanner.engine import Scanner
from odda.scanner.guide import load_rule_guide
from odda.scanner.userrules import SCOPES, RuleManager

# See ADR-0019, ADR-0020.
_PIPELINE_MIN_NAMES = 2
_REPEAT_MIN = 2


class ToolParamError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def odda_tool(fn):

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (BrowserOperationError, ToolParamError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


@dataclass
class OddaState:
    proxy: ProxyServer
    browser: BrowserManager
    rules: RuleManager


_config_overrides: dict[str, Any] = {}


def set_config_overrides(overrides: dict[str, Any]) -> None:
    _config_overrides.clear()
    _config_overrides.update(overrides)


@asynccontextmanager
async def odda_lifespan(
    server: MCPServer[OddaState],  # noqa: ARG001 — SDK lifespan signature
) -> AsyncIterator[OddaState]:
    data_dir = project_data_dir()
    flowstore.set_data_dir(data_dir)
    # See ADR-0042.
    config = load_config(_config_overrides, project_path=data_dir / CONFIG_FILE_NAME)
    proxy = ProxyServer()
    browser = BrowserManager(proxy=proxy, config=config)
    warm_driver()
    proxy.restore_scripts_on_boot()
    # See ADR-0034, ADR-0036.
    scanner = Scanner(data_dir)
    rules = RuleManager(
        scanner,
        global_dir=global_rules_dir(),
        project_dir=data_dir / "rules",
        disabled_dir=data_dir / "scanner" / "disabled",
    )
    rules.restore_on_boot()
    flowstore.get_writer().set_observer(scanner.observe)
    scanner_task = asyncio.create_task(scanner.run())
    # See ADR-0042.
    stale_task: asyncio.Task[None] | None = None
    if config.stale_browser_timeout > 0:
        stale_task = asyncio.create_task(
            browser.run_stale_reaper(config.stale_browser_timeout)
        )
    try:
        yield OddaState(proxy=proxy, browser=browser, rules=rules)
    finally:
        if stale_task is not None:
            stale_task.cancel()
            with suppress(asyncio.CancelledError):
                await stale_task
        scanner_task.cancel()
        with suppress(asyncio.CancelledError):
            await scanner_task
        flowstore.get_writer().set_observer(None)
        await browser.close_all()
        await proxy.shutdown()


mcp_server = MCPServer[OddaState](
    "odda",
    version=__version__,
    description="Browser automation and HTTP traffic capture for agents.",
    lifespan=odda_lifespan,
    instructions=(
        "All traffic that goes through the proxy (including browser "
        "traffic) is captured as flows under .odda/flows/ — driving "
        "the browser is capture. flows.jsonl is the append-only "
        "index. Response bodies are stored decoded. WebSocket "
        "traffic: the 101 handshake is a normal flow record with "
        '"ws": true, and each message appends one line to '
        'flows/<id>/ws.jsonl ({"from": client|server, "opcode": '
        'text|binary, "content", "ts"}; binary frames are '
        "latin-1-escaped — embedded ASCII stays greppable and every "
        'byte round-trips via content.encode("latin-1")) until the '
        "terminal close record. Captured flows "
        "are passively scanned while the session lives and findings "
        "append to .odda/scanner/findings.jsonl; flows that predate "
        "the session are never scanned."
    ),
)


@mcp_server.tool()
@odda_tool
async def browser_open(
    *,
    browser_id: str | None = None,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Open a browser: fresh, or reopen a closed record (same id, same profile)."""
    return await ctx.request_context.lifespan_context.browser.open(
        browser_id=browser_id
    )


@mcp_server.tool()
@odda_tool
async def browser_close(browser_id: str, *, ctx: Context[OddaState]) -> dict[str, Any]:
    """Close a browser instance by id."""
    return await ctx.request_context.lifespan_context.browser.close_instance(browser_id)


@mcp_server.tool()
@odda_tool
async def browser_list(
    *,
    ctx: Context[OddaState],
) -> list[dict[str, Any]]:
    """List every browser record in the data dir, alphabetically."""
    return ctx.request_context.lifespan_context.browser.list_instances()


@mcp_server.tool()
@odda_tool
async def tabs_list(
    browser_id: str | None = None, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """List open tabs, optionally filtered to one browser."""
    return await ctx.request_context.lifespan_context.browser.list_tabs(browser_id)


@mcp_server.tool()
@odda_tool
async def tabs_open(
    browser_id: str,
    url: str | None = None,
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Open a new tab in a browser, optionally navigating to a URL."""
    return await ctx.request_context.lifespan_context.browser.open_tab(browser_id, url)


@mcp_server.tool()
@odda_tool
async def tabs_close(
    browser_id: str, tab_id: int, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Close a tab in a browser."""
    return await ctx.request_context.lifespan_context.browser.close_tab(
        browser_id, tab_id
    )


@mcp_server.tool()
@odda_tool
async def dialog_handle(
    browser_id: str,
    tab_id: int,
    action: Literal["accept", "dismiss"],
    prompt_text: str | None = None,
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Accept or dismiss the tab's open alert/confirm/prompt dialog."""
    return await ctx.request_context.lifespan_context.browser.handle_dialog(
        browser_id, tab_id, action, prompt_text
    )


@mcp_server.tool()
@odda_tool
async def navigate(
    browser_id: str,
    tab_id: int,
    url: str,
    timeout: float = 30.0,
    wait_until: str = "load",
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Navigate an existing tab to a URL."""
    if wait_until not in NAVIGATE_WAIT_UNTIL_EVENTS:
        raise ToolParamError(
            f"wait_until must be one of {', '.join(NAVIGATE_WAIT_UNTIL_EVENTS)}, "
            f"got {wait_until!r}"
        )
    return await ctx.request_context.lifespan_context.browser.navigate(
        browser_id,
        tab_id,
        url,
        timeout_ms=timeout * 1000,
        wait_until=wait_until,
    )


def _read_payload_file(file: str) -> str:
    path = Path(file)
    if not path.is_file():
        raise ToolParamError(f"File not found: {file}")
    return path.read_text(encoding="utf-8")


@mcp_server.tool()
@odda_tool
async def eval(
    browser_id: str,
    tab_id: int,
    js: str | None = None,
    file: str | None = None,
    *,
    ctx: Context[OddaState],
) -> Any:
    """Execute JavaScript in the target tab and return the raw value.

    js is an expression, not statements; returned Promises are
    awaited; the return value must be JSON-serializable.
    """
    if js is not None and file is not None:
        raise ToolParamError("Provide either js or file, not both")
    if js is None and file is None:
        raise ToolParamError("Provide js <code> or file <path>")
    if file is not None:
        js = _read_payload_file(file)
    return await ctx.request_context.lifespan_context.browser.eval_js(
        browser_id, tab_id, js
    )


@mcp_server.tool()
@odda_tool
async def wait_for(
    browser_id: str,
    tab_id: int,
    expression: str,
    timeout: float = 30.0,
    *,
    ctx: Context[OddaState],
) -> Any:
    """Poll a JS expression until it's truthy or timeout in the target tab."""
    return await ctx.request_context.lifespan_context.browser.wait_for(
        browser_id, tab_id, expression, timeout_ms=timeout * 1000
    )


@mcp_server.tool(structured_output=False)
@odda_tool
async def screenshot(
    browser_id: str,
    tab_id: int,
    output: str | None = None,
    *,
    annotate: bool = False,
    return_image: bool = True,
    ctx: Context[OddaState],
) -> str | CallToolResult:
    """Capture a JPEG of the target tab's viewport."""
    path, legend = await ctx.request_context.lifespan_context.browser.screenshot(
        browser_id, tab_id, output, annotate=annotate
    )
    blocks: list[TextContent | ImageContent] = [TextContent(type="text", text=path)]
    if legend is not None:
        blocks.append(TextContent(type="text", text=legend))
    if return_image:
        blocks.append(
            ImageContent(
                type="image",
                data=base64.b64encode(Path(path).read_bytes()).decode(),
                mimeType="image/jpeg",
            )
        )
    if len(blocks) == 1:
        return path
    return CallToolResult(content=blocks)


@mcp_server.tool()
@odda_tool
async def page_snapshot(
    browser_id: str,
    tab_id: int,
    depth: int | None = None,
    *,
    boxes: bool = False,
    diff: bool = False,
    ctx: Context[OddaState],
) -> str:
    """Take an agent-readable snapshot of the page's accessibility tree."""
    return await ctx.request_context.lifespan_context.browser.page_snapshot(
        browser_id,
        tab_id,
        depth=depth,
        boxes=boxes,
        diff=diff,
    )


@mcp_server.tool()
@odda_tool
async def page_find(
    browser_id: str,
    tab_id: int,
    regex: str,
    *,
    boxes: bool = False,
    ctx: Context[OddaState],
) -> str:
    """Search the page's accessibility snapshot for a regex."""
    try:
        pattern = re.compile(regex)
    except re.error as exc:
        raise ToolParamError(f"Invalid regex: {exc}") from exc
    return await ctx.request_context.lifespan_context.browser.page_find(
        browser_id, tab_id, pattern, boxes=boxes
    )


@mcp_server.tool()
@odda_tool
async def page_click(  # noqa: PLR0913 — targeting + ref/coords union is the tool's contract
    browser_id: str,
    tab_id: int,
    ref: str | None = None,
    x: float | None = None,
    y: float | None = None,
    timeout: float = 5.0,
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Click the element identified by ref, or at viewport coordinates."""
    browser = ctx.request_context.lifespan_context.browser
    if x is not None or y is not None:
        if ref is not None:
            raise ToolParamError("Provide either x/y or ref, not both")
        if x is None or y is None:
            raise ToolParamError("Provide both x and y for coordinate mode")
        return await browser.page_click_coords(browser_id, tab_id, x=x, y=y)
    if ref is None:
        raise ToolParamError("Provide either ref or x/y coordinates")
    return await browser.page_click(browser_id, tab_id, ref, timeout_ms=timeout * 1000)


@mcp_server.tool()
@odda_tool
async def page_fill(  # noqa: PLR0913 — value xor file is the tool's contract
    browser_id: str,
    tab_id: int,
    ref: str,
    value: str | None = None,
    file: str | None = None,
    timeout: float = 5.0,
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Fill the element identified by ref with value."""
    if value is not None and file is not None:
        raise ToolParamError("Provide either value or file, not both")
    if value is None and file is None:
        raise ToolParamError("Provide value <string> or file <path>")
    if file is not None:
        value = _read_payload_file(file)
    return await ctx.request_context.lifespan_context.browser.page_fill(
        browser_id, tab_id, ref, value, timeout_ms=timeout * 1000
    )


@mcp_server.tool()
@odda_tool
async def page_hover(  # noqa: PLR0913 — targeting + ref/coords union is the tool's contract
    browser_id: str,
    tab_id: int,
    ref: str | None = None,
    x: float | None = None,
    y: float | None = None,
    timeout: float = 5.0,
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Hover the element identified by ref, or at viewport coordinates."""
    browser = ctx.request_context.lifespan_context.browser
    if x is not None or y is not None:
        if ref is not None:
            raise ToolParamError("Provide either x/y or ref, not both")
        if x is None or y is None:
            raise ToolParamError("Provide both x and y for coordinate mode")
        return await browser.page_hover_coords(browser_id, tab_id, x=x, y=y)
    if ref is None:
        raise ToolParamError("Provide either ref or x/y coordinates")
    return await browser.page_hover(browser_id, tab_id, ref, timeout_ms=timeout * 1000)


@mcp_server.tool()
@odda_tool
async def page_upload(
    browser_id: str,
    tab_id: int,
    ref: str,
    files: list[str],
    timeout: float = 5.0,
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Upload local files to the file input identified by ref."""
    return await ctx.request_context.lifespan_context.browser.page_upload(
        browser_id, tab_id, ref, files, timeout_ms=timeout * 1000
    )


@mcp_server.tool()
@odda_tool
async def event_listeners(
    browser_id: str, tab_id: int, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """List JavaScript event listeners on window and document in the target tab."""
    return await ctx.request_context.lifespan_context.browser.list_event_listeners(
        browser_id, tab_id
    )


@mcp_server.tool()
@odda_tool
async def cookies_list(
    browser_id: str, url: str | None = None, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """Return a browser's cookie jar as one JSON array."""
    return await ctx.request_context.lifespan_context.browser.cookies_list(
        browser_id, url
    )


@mcp_server.tool()
@odda_tool
async def cookies_clear(
    browser_id: str,
    name: str | None = None,
    domain: str | None = None,
    path: str | None = None,
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Delete cookies from a browser's jar."""
    return await ctx.request_context.lifespan_context.browser.cookies_clear(
        browser_id, name, domain, path
    )


@mcp_server.tool()
@odda_tool
async def cookies_set(
    browser_id: str,
    cookies: list[dict[str, Any]],
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Plant cookies into a browser's jar."""
    for i, cookie in enumerate(cookies):
        if (
            not isinstance(cookie, dict)
            or not cookie.get("name")
            or "value" not in cookie
        ):
            raise ToolParamError(f"cookies[{i}] needs name and value")
        if "url" not in cookie and ("domain" not in cookie or "path" not in cookie):
            raise ToolParamError(f"cookies[{i}] needs url or domain and path")
        if "url" in cookie and "domain" in cookie:
            raise ToolParamError(f"cookies[{i}] takes url or domain and path, not both")
    return await ctx.request_context.lifespan_context.browser.cookies_set(
        browser_id, cookies
    )


@mcp_server.tool()
@odda_tool
async def version() -> dict[str, Any]:
    """Return the odda version."""
    return {"version": __version__}


@mcp_server.tool()
@odda_tool
async def request_clone(
    flow_id: str,
    name: str,
    *,
    force: bool = False,
    ctx: Context[OddaState],  # noqa: ARG001
) -> dict[str, Any]:
    """Clone a captured flow's request into an editable request."""
    return clone_request(flow_id, name, force=force)


@mcp_server.tool()
@odda_tool
async def request_new(  # noqa: PLR0913 — the tool mirrors request new's full flag surface
    name: str,
    host: str,
    protocol: str = "https",
    port: int | None = None,
    line_terminator: list[int] | None = None,
    *,
    force: bool = False,
    ctx: Context[OddaState],  # noqa: ARG001
) -> dict[str, Any]:
    """Create a new empty editable request.

    Writes .odda/requests/<name>/request plus a meta.json sidecar.
    HTTP/1.1 request files are wire-faithful: their bytes go on the
    socket verbatim. HTTP/2 request files are frame-sources: parsed
    into header values and translated to H2 frames, so the line
    terminator (line_terminator, default CRLF) decides how the file
    splits into header values; a Host header translates to
    :authority.
    """
    lt_bytes = bytes(line_terminator) if line_terminator is not None else b"\r\n"
    return new_request(
        name,
        host=host,
        protocol=protocol,
        port=port,
        line_terminator=lt_bytes,
        force=force,
    )


@mcp_server.tool()
@odda_tool
async def request_send(  # noqa: PLR0913 — single/pipeline/repeat union is the tool's contract
    name: str | None = None,
    names: list[str] | None = None,
    repeat: int | None = None,
    timeout: float = 30.0,
    *,
    fix_content_length: bool = False,
    insecure: bool = False,
    pipelining: bool = False,
    ctx: Context[OddaState],  # noqa: ARG001
) -> dict[str, Any] | list[dict[str, Any]]:
    """Send editable request(s) and record each response as a flow.

    Arity selects the mode: one name → one flow; names (two or
    more) → one connection, in order (HTTP/1.1 sequential
    keep-alive, or pipelining for send-all-then-read-all; HTTP/2
    concurrent streams); repeat >= 2 with one name → N concurrent
    copies. Errors are recorded as error flows, not tool errors.
    Requests dial direct from this machine, never through odda's
    proxy.
    """
    if repeat is not None and names is not None:
        raise ToolError(
            "repeat is single-name only; use repeat with one name, "
            "or names (multi-name) without repeat (the combo is ambiguous)"
        )
    if repeat is not None and pipelining:
        raise ToolError(
            "repeat is concurrent; pipelining is H1 multi-name "
            "sequential — they cannot be combined"
        )
    if names is not None and len(names) >= _PIPELINE_MIN_NAMES:
        return await send_request_pipeline(
            names,
            fix_content_length=fix_content_length,
            timeout=timeout,
            insecure=insecure,
            pipelining=pipelining,
        )
    if repeat is not None and repeat >= _REPEAT_MIN:
        return await send_request_repeat(
            name,
            repeat,
            fix_content_length=fix_content_length,
            timeout=timeout,
            insecure=insecure,
        )
    if name is None:
        raise ToolError("Provide name (single or repeat) or names (pipeline)")
    return await send_request(
        name,
        fix_content_length=fix_content_length,
        timeout=timeout,
        insecure=insecure,
    )


@mcp_server.tool()
@odda_tool
async def proxy_url(*, ctx: Context[OddaState]) -> str:
    """Return the HTTP proxy URL."""
    return ctx.request_context.lifespan_context.proxy.proxy_url


@mcp_server.tool()
@odda_tool
async def proxy_upstream_set(
    url: str,
    auth: str | None = None,
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Route the proxy's upstream traffic through a chained HTTP(S) proxy.

    Loopback targets are unreachable via a remote upstream.
    """
    return await ctx.request_context.lifespan_context.proxy.set_upstream(url, auth)


@mcp_server.tool()
@odda_tool
async def proxy_upstream_clear(*, ctx: Context[OddaState]) -> dict[str, Any]:
    """Remove the upstream proxy and return to direct egress."""
    return await ctx.request_context.lifespan_context.proxy.clear_upstream()


@mcp_server.tool()
@odda_tool
async def proxy_upstream_get(*, ctx: Context[OddaState]) -> dict[str, Any]:
    """Report the current upstream proxy configuration."""
    return ctx.request_context.lifespan_context.proxy.upstream_state()


@mcp_server.tool()
@odda_tool
async def coverage_start(
    browser_id: str, tab_id: int, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Enable block-level coverage on the target tab."""
    return await ctx.request_context.lifespan_context.browser.coverage_start(
        browser_id, tab_id
    )


@mcp_server.tool()
@odda_tool
async def coverage_snapshot(
    browser_id: str, tab_id: int, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Read per-block hit counts on the target tab without stopping."""
    return await ctx.request_context.lifespan_context.browser.coverage_snapshot(
        browser_id, tab_id
    )


@mcp_server.tool()
@odda_tool
async def coverage_stop(
    browser_id: str, tab_id: int, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Take a final coverage snapshot and stop recording."""
    return await ctx.request_context.lifespan_context.browser.coverage_stop(
        browser_id, tab_id
    )


@mcp_server.tool()
@odda_tool
async def wrap_calls_add(
    browser_id: str, name: str, expr: str, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Install a call wrap on a named function, even one defined later."""
    return await ctx.request_context.lifespan_context.browser.wrap_calls_add(
        browser_id, name, expr
    )


@mcp_server.tool()
@odda_tool
async def wrap_access_add(
    browser_id: str, name: str, expr: str, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Install an access wrap on a property (effective next navigation).

    Accessors, data slots, and properties the page defines later all
    record; frozen or pinned targets can't be wrapped.
    """
    return await ctx.request_context.lifespan_context.browser.wrap_access_add(
        browser_id, name, expr
    )


@mcp_server.tool()
@odda_tool
async def wrap_list(
    browser_id: str, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """List installed wraps (name, type, expr) for the browser."""
    return await ctx.request_context.lifespan_context.browser.wrap_list(browser_id)


@mcp_server.tool()
@odda_tool
async def wrap_remove(
    browser_id: str, name: str, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Remove a wrap; it stops recording on future navigations."""
    return await ctx.request_context.lifespan_context.browser.wrap_remove(
        browser_id, name
    )


@mcp_server.tool()
@odda_tool
async def wrap_dump(
    browser_id: str, tab_id: int, name: str | None = None, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """Read the per-tab wrap record array."""
    return await ctx.request_context.lifespan_context.browser.wrap_dump(
        browser_id, tab_id, name
    )


@mcp_server.tool()
@odda_tool
async def wrap_clear(
    browser_id: str, tab_id: int, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Zero the per-tab wrap record array without navigating."""
    return await ctx.request_context.lifespan_context.browser.wrap_clear(
        browser_id, tab_id
    )


@mcp_server.tool()
@odda_tool
async def logpoint_add(  # noqa: PLR0913 — targeting + url/line/col/expr is the tool's contract
    browser_id: str,
    tab_id: int,
    url: str,
    line: int,
    col: int,
    expr: str,
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Plant a non-pausing logpoint at a source location (0-based line/col)."""
    return await ctx.request_context.lifespan_context.browser.logpoint_add(
        browser_id, tab_id, url, line, col, expr
    )


@mcp_server.tool()
@odda_tool
async def logpoint_list(
    browser_id: str, tab_id: int, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """List planted logpoints on the target tab."""
    return await ctx.request_context.lifespan_context.browser.logpoint_list(
        browser_id, tab_id
    )


@mcp_server.tool()
@odda_tool
async def logpoint_remove(
    browser_id: str, tab_id: int, lp_id: str, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Remove a planted logpoint by its id."""
    return await ctx.request_context.lifespan_context.browser.logpoint_remove(
        browser_id, tab_id, lp_id
    )


@mcp_server.tool()
@odda_tool
async def logpoint_dump(
    browser_id: str, tab_id: int, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """Read the per-tab logpoint record array."""
    return await ctx.request_context.lifespan_context.browser.logpoint_dump(
        browser_id, tab_id
    )


@mcp_server.tool()
@odda_tool
async def logpoint_clear(
    browser_id: str, tab_id: int, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Zero the per-tab logpoint record array without navigating."""
    return await ctx.request_context.lifespan_context.browser.logpoint_clear(
        browser_id, tab_id
    )


def _read_install_source(file: str | None, source: str | None, what: str) -> str:
    if file is not None and source is not None:
        raise ToolParamError("Provide either file or source, not both")
    if file is None and source is None:
        raise ToolParamError(f"Provide file <path> or source <{what}>")
    if file is not None:
        return _read_payload_file(file)
    return source  # type: ignore[return-value]


@mcp_server.tool()
@odda_tool
async def userscript_install(
    browser_id: str,
    name: str,
    file: str | None = None,
    source: str | None = None,
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Install a userscript (auto-runs at document_start, main world)."""
    js = _read_install_source(file, source, "js")
    if not js.strip():
        raise ToolParamError("source is empty")
    return await ctx.request_context.lifespan_context.browser.install_userscript(
        browser_id, name, js
    )


@mcp_server.tool()
@odda_tool
async def userscript_list(
    browser_id: str, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """List installed userscripts for one browser (name, size)."""
    return ctx.request_context.lifespan_context.browser.list_userscripts(browser_id)


@mcp_server.tool()
@odda_tool
async def userscript_remove(
    browser_id: str, name: str, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Remove a userscript; it stops running on new navigations."""
    return await ctx.request_context.lifespan_context.browser.remove_userscript(
        browser_id, name
    )


@mcp_server.tool()
@odda_tool
async def proxy_script_install(
    name: str,
    file: str | None = None,
    source: str | None = None,
    *,
    force: bool = False,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Install a proxy-script (mitmproxy ``-s`` addon) from file or source."""
    py = _read_install_source(file, source, "py")
    if not py.strip():
        raise ToolParamError("source is empty")
    if not force and proxyscript.name_exists(name):
        raise ToolParamError(
            f"Proxy-script '{name}' already installed. Use force to overwrite."
        )
    return ctx.request_context.lifespan_context.proxy.install_script(name, py)


@mcp_server.tool()
@odda_tool
async def proxy_script_list(*, ctx: Context[OddaState]) -> list[dict[str, Any]]:
    """List installed proxy-scripts (from disk, annotated with live state)."""
    return ctx.request_context.lifespan_context.proxy.list_scripts()


@mcp_server.tool()
@odda_tool
async def proxy_script_remove(name: str, *, ctx: Context[OddaState]) -> dict[str, Any]:
    """Remove a proxy-script: live instance + on-disk source."""
    return ctx.request_context.lifespan_context.proxy.remove_script(name)


# See ADR-0038.
@mcp_server.resource(
    "odda://scanner/rule-guide",
    name="rule-guide",
    title="Scanner rule guide",
    description=(
        "The complete rule-authoring guide: the rule-file shape, every "
        "matcher and gate, the FlowView API, worked examples of both "
        "rule styles, the severity scale, and install semantics."
    ),
    mime_type="text/markdown",
)
def scanner_rule_guide() -> str:
    return load_rule_guide()


_RULE_INSTALL_DOC = (
    "Install a passive-scanner rule slot from file or source into this "
    "project's rules dir (.odda/rules/), or with scope='global' into "
    "the global rules dir under the odda config home (fires in every "
    "project on the machine). Installed rules scan flows captured "
    "after the install, never retroactively. Installing from a file "
    "path copies the file's sibling non-.py assets (datasets) into "
    "the slot; rule_disable suppresses an installed rule without "
    "removing it. The full authoring guide is the odda://scanner/"
    "rule-guide resource."
)


@mcp_server.tool(description=_RULE_INSTALL_DOC)
@odda_tool
async def rule_install(
    name: str,
    file: str | None = None,
    source: str | None = None,
    *,
    force: bool = False,
    scope: str = "project",
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Install a passive-scanner rule slot from file or source."""
    py = _read_install_source(file, source, "py")
    if not py.strip():
        raise ToolParamError("source is empty")
    if scope not in SCOPES:
        raise ToolParamError(f"scope must be one of {', '.join(SCOPES)}")
    rules = ctx.request_context.lifespan_context.rules
    if not force and rules.name_exists(name, scope):
        raise ToolParamError(
            f"Rule slot '{name}' already installed. Use force to overwrite."
        )
    return await rules.install(name, py, assets_from=file, scope=scope)


@mcp_server.tool()
@odda_tool
async def rule_list(*, ctx: Context[OddaState]) -> list[dict[str, Any]]:
    """List every installed rule: slot, scope, and state."""
    return ctx.request_context.lifespan_context.rules.list_rules()


@mcp_server.tool()
@odda_tool
async def rule_remove(
    name: str, *, scope: str = "project", ctx: Context[OddaState]
) -> dict[str, Any]:
    """Remove a rule slot.

    Live rules stop, past findings remain — the project rules dir by
    default, the global rules dir with scope='global'.
    """
    if scope not in SCOPES:
        raise ToolParamError(f"scope must be one of {', '.join(SCOPES)}")
    return await ctx.request_context.lifespan_context.rules.remove(name, scope=scope)


_RULE_DISABLE_DOC = (
    "Disable an installed rule for this project — a slot name disables "
    "every rule in the slot, a rule id disables that one rule. Disabled "
    "rules stay installed and listed, stop matching flows captured after "
    "the disable, and rule_enable restores them."
)
_RULE_ENABLE_DOC = (
    "Restore a disabled rule — by the slot name or rule id it was "
    "disabled under. Its rules match flows captured after the enable."
)


def _rule_target(name: str | None, rule_id: str | None) -> tuple[str, str]:
    if (name is None) == (rule_id is None):
        raise ToolParamError("Pass exactly one of name or id")
    return ("name", name) if name is not None else ("id", rule_id)


@mcp_server.tool(description=_RULE_DISABLE_DOC)
@odda_tool
async def rule_disable(
    name: str | None = None,
    id: str | None = None,
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Disable an installed rule slot (by name) or rule (by id)."""
    kind, value = _rule_target(name, id)
    return await ctx.request_context.lifespan_context.rules.disable(kind, value)


@mcp_server.tool(description=_RULE_ENABLE_DOC)
@odda_tool
async def rule_enable(
    name: str | None = None,
    id: str | None = None,
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Re-enable a disabled rule slot (by name) or rule (by id)."""
    kind, value = _rule_target(name, id)
    return await ctx.request_context.lifespan_context.rules.enable(kind, value)


def main() -> None:
    mcp_server.run()


if __name__ == "__main__":
    main()
