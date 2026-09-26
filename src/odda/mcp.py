"""MCP server: stdio surface exposing odda's tools.

``odda mcp`` boots this module's :data:`mcp_server` as the *only* odda
process — the MCP lifespan replaces ``OddaServer`` as the owner of the
proxy, the browser manager, and the flowstore data dir. Tool handlers
are ports of today's ``OddaServer.method_*`` bodies; the result/error
contract is the one pinned in ticket #03 (``.scratch/odda-mcp/issues/
03-targeting-result-error-contract.md``):

- explicit required ``browser_id`` / ``tab_id`` params (no positional);
- natural return types, text-first delivery — ``dict`` passes through
  (structuredContent is the dict itself, which omp-style clients dedupe
  against the text block); the str/list/union tools pass
  ``structured_output=False`` — no structured channel, which omp would
  re-append — and keep their declared types: str tools return raw text,
  list/union tools wrap in ``_json_result`` (one JSON document, since
  the SDK's per-item list rendering loses array-ness) (ADR 0023);
- one ``@odda_tool`` decorator converts ``BrowserOperationError``,
  ``ToolParamError``, and the request/userscript/proxy-script
  libraries' ``ValueError``s to ``ToolError`` (message verbatim);
  anything else stays an SDK crash logged to stderr.
"""

from __future__ import annotations

import base64
import functools
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

# Context must be imported at runtime: the SDK resolves tool
# annotations via get_type_hints at add_tool time, so a
# TYPE_CHECKING-only import breaks Context-kwarg detection. TC002
# survives ``flake8-type-checking.runtime-evaluated-decorators`` here
# because ruff ignores that setting under PEP 563
# (``from __future__ import annotations``); verified against ruff 0.15.17.
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context  # noqa: TC002
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent

from odda import NAVIGATE_WAIT_UNTIL_EVENTS, __version__, flowstore, proxyscript
from odda.browser import BrowserManager, BrowserOperationError
from odda.proxy import ProxyServer
from odda.request import (
    clone as clone_request,
    new as new_request,
    send as send_request,
    send_pipeline as send_request_pipeline,
    send_repeat as send_request_repeat,
)

# Two or more names selects the multi-name pipeline mode (ADR-0019);
# repeat >= 2 selects the concurrent-send mode (ADR-0020) — the same
# arity thresholds the CLI's flag combination enforced.
_PIPELINE_MIN_NAMES = 2
_REPEAT_MIN = 2


def _json_result(value: Any) -> CallToolResult:
    """Wire a list/union tool result as ONE JSON document (ADR 0023).

    The SDK's list handling emits one text block per item — a 1-item
    list loses its array-ness and an empty list emits nothing — and a
    structured channel would be re-appended by omp. An explicit
    CallToolResult passes through convert_result verbatim.
    """
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(value, indent=2))]
    )


class ToolParamError(Exception):
    """Handler-level validation error (a bad tool argument combination).

    The successor of the JSON-RPC layer's INVALID_PARAMS: the tool
    itself is the surface now, so handler-side validation raises this
    instead of ``rpc.JsonRpcError``. :func:`odda_tool` converts it to
    the SDK's ``ToolError`` with the message verbatim.
    """

    def __init__(self, message: str) -> None:
        """Initialize with a short human-readable message."""
        super().__init__(message)
        self.message = message


if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def odda_tool(fn):
    """Convert odda's anticipated errors to SDK ``ToolError``.

    ``BrowserOperationError`` (library messages: tab not found, ref did
    not resolve, closed during …), ``ToolParamError`` (handler
    validation: source is empty, ref-or-coords conflicts), and
    ``ValueError`` (the request/userscript/proxy-script libraries'
    anticipated validation: name collisions, not-found, empty request
    file, pre-emptive pipeline/repeat rejections — messages the old
    JSON-RPC dispatch surfaced via its catch-all) all become
    ``ToolError`` with the message verbatim — without this the model
    sees a content-free crash and loses every self-correction message.
    Everything else stays a crash: the SDK's ``UnexpectedToolError``
    path, traceback to stderr, per the map's log decision.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (BrowserOperationError, ToolParamError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


@dataclass
class OddaState:
    """What the MCP lifespan owns: proxy, browser manager, flowstore dir."""

    proxy: ProxyServer
    browser: BrowserManager


def resolve_data_dir() -> Path:
    """Resolve the data dir for this MCP process.

    ``.odda/`` under the current working directory (ADR 0017: the
    directory is remembered, not created — it appears lazily on first
    state write).
    """
    return Path(".odda").resolve()


@asynccontextmanager
async def odda_lifespan(
    server: MCPServer[OddaState],  # noqa: ARG001 — SDK lifespan signature
) -> AsyncIterator[OddaState]:
    """Boot odda with the MCP process; tear down when the client leaves.

    Process start *is* server boot (ticket #02's verified seam): the
    same ownership order ``OddaServer.run`` uses — ``set_data_dir`` →
    ``ProxyServer()`` → ``BrowserManager(proxy=…)`` — minus the Unix
    socket, signal handlers, and parent watch that die with the CLI.
    Proxy-scripts are restored from disk before the yield (ticket #12):
    the yield is the boot barrier — ``Server.run`` enters the lifespan
    before driving the message loop (mcp 2.1.1 lowlevel server.py:
    ``async with self.lifespan(...)`` precedes ``serve_dual_era_loop``),
    and the in-process ``Client(server)`` path enters the lifespan the
    same way before its dispatcher exists (mcp/client/client.py
    ``_connect_inproc``). So no ``tools/call`` can arrive mid-restore:
    restore runs to completion inside the synchronous startup block,
    before the first frame is even read.
    """
    data_dir = resolve_data_dir()
    flowstore.set_data_dir(data_dir)
    proxy = ProxyServer()
    browser = BrowserManager(proxy=proxy)
    # After BrowserManager, mirroring OddaServer.run's order: after
    # set_data_dir so the manager sees the right .odda path, after
    # ProxyServer so the DumpMaster addon chain is ready. A failing
    # proxy-script is logged and skipped, never blocking boot.
    proxy.restore_scripts_on_boot()
    try:
        yield OddaState(proxy=proxy, browser=browser)
    finally:
        await browser.close_all()
        await proxy.shutdown()


mcp_server = MCPServer[OddaState](
    "odda",
    version=__version__,
    # description → serverInfo block in the initialize result: the
    # short "what is this server" label. instructions →
    # initialize.instructions: the detailed orientation brief.
    description="Browser automation and HTTP traffic capture for agents.",
    lifespan=odda_lifespan,
    instructions=(
        "All traffic that goes through the proxy (including browser "
        "traffic) is captured as flows under .odda/flows/ — driving "
        "the browser is capture. flows.jsonl is the append-only "
        "index. Response bodies are stored decoded."
    ),
)


# --- browser lifecycle ---


@mcp_server.tool()
@odda_tool
async def browser_open(
    *,
    browser_id: str | None = None,
    headless: bool = True,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Open a browser: fresh, or reopen a closed record (same id, same profile)."""
    return await ctx.request_context.lifespan_context.browser.open(
        browser_id=browser_id, headless=headless
    )


@mcp_server.tool()
@odda_tool
async def browser_close(browser_id: str, *, ctx: Context[OddaState]) -> dict[str, Any]:
    """Close a browser instance by id."""
    return await ctx.request_context.lifespan_context.browser.close_instance(browser_id)


@mcp_server.tool(structured_output=False)
@odda_tool
async def browser_list(
    *,
    ctx: Context[OddaState],
) -> list[dict[str, Any]]:
    """List every browser record in the data dir, alphabetically."""
    return _json_result(ctx.request_context.lifespan_context.browser.list_instances())


# --- tab lifecycle ---


@mcp_server.tool(structured_output=False)
@odda_tool
async def tabs_list(
    browser_id: str | None = None, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """List open tabs, optionally filtered to one browser."""
    return _json_result(
        await ctx.request_context.lifespan_context.browser.list_tabs(browser_id)
    )


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


# --- page control ---


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
    """Read a literal-vs-file payload: validate existence, read as UTF-8.

    The exclusivity/required check lives in the caller (its param names
    differ per tool); this is just the shared file leg.
    """
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


# --- page interaction (snapshot + ref-driven actions) ---


@mcp_server.tool(structured_output=False)
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


@mcp_server.tool(structured_output=False)
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


# --- introspection ---


@mcp_server.tool(structured_output=False)
@odda_tool
async def event_listeners(
    browser_id: str, tab_id: int, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """List JavaScript event listeners on window and document in the target tab."""
    return _json_result(
        await ctx.request_context.lifespan_context.browser.list_event_listeners(
            browser_id, tab_id
        )
    )


@mcp_server.tool(structured_output=False)
@odda_tool
async def cookies_list(
    browser_id: str, url: str | None = None, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """Return a browser's cookie jar as one JSON array."""
    return _json_result(
        await ctx.request_context.lifespan_context.browser.cookies_list(browser_id, url)
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


# --- request crafting (raw HTTP) ---


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


@mcp_server.tool(structured_output=False)
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
        return _json_result(
            await send_request_pipeline(
                names,
                fix_content_length=fix_content_length,
                timeout=timeout,
                insecure=insecure,
                pipelining=pipelining,
            )
        )
    if repeat is not None and repeat >= _REPEAT_MIN:
        return _json_result(
            await send_request_repeat(
                name,
                repeat,
                fix_content_length=fix_content_length,
                timeout=timeout,
                insecure=insecure,
            )
        )
    if name is None:
        raise ToolError("Provide name (single or repeat) or names (pipeline)")
    return _json_result(
        await send_request(
            name,
            fix_content_length=fix_content_length,
            timeout=timeout,
            insecure=insecure,
        )
    )


@mcp_server.tool(structured_output=False)
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


# --- dynamic analysis: coverage ---


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


# --- dynamic analysis: wraps ---


@mcp_server.tool()
@odda_tool
async def wrap_calls_add(
    browser_id: str, name: str, expr: str, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Install a call wrap on a named function; effective on the next navigation."""
    return await ctx.request_context.lifespan_context.browser.wrap_calls_add(
        browser_id, name, expr
    )


@mcp_server.tool()
@odda_tool
async def wrap_access_add(
    browser_id: str, name: str, expr: str, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Install an access wrap on a property accessor (effective next navigation)."""
    return await ctx.request_context.lifespan_context.browser.wrap_access_add(
        browser_id, name, expr
    )


@mcp_server.tool(structured_output=False)
@odda_tool
async def wrap_list(
    browser_id: str, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """List installed wraps (name, type, expr) for the browser."""
    return _json_result(
        await ctx.request_context.lifespan_context.browser.wrap_list(browser_id)
    )


@mcp_server.tool()
@odda_tool
async def wrap_remove(
    browser_id: str, name: str, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Remove a wrap; it stops recording on future navigations."""
    return await ctx.request_context.lifespan_context.browser.wrap_remove(
        browser_id, name
    )


@mcp_server.tool(structured_output=False)
@odda_tool
async def wrap_dump(
    browser_id: str, tab_id: int, name: str | None = None, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """Read the per-tab wrap record array."""
    return _json_result(
        await ctx.request_context.lifespan_context.browser.wrap_dump(
            browser_id, tab_id, name
        )
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


# --- dynamic analysis: logpoints ---


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


@mcp_server.tool(structured_output=False)
@odda_tool
async def logpoint_list(
    browser_id: str, tab_id: int, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """List planted logpoints on the target tab."""
    return _json_result(
        await ctx.request_context.lifespan_context.browser.logpoint_list(
            browser_id, tab_id
        )
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


@mcp_server.tool(structured_output=False)
@odda_tool
async def logpoint_dump(
    browser_id: str, tab_id: int, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """Read the per-tab logpoint record array."""
    return _json_result(
        await ctx.request_context.lifespan_context.browser.logpoint_dump(
            browser_id, tab_id
        )
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


# --- dynamic analysis: userscripts ---


def _read_install_source(file: str | None, source: str | None, what: str) -> str:
    """Resolve an install payload's source: file xor inline, both validated.

    The CLI's flag checks move here — the tool is the surface now and
    a missing file must read as odda's message, not an SDK crash.
    """
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


@mcp_server.tool(structured_output=False)
@odda_tool
async def userscript_list(
    browser_id: str, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """List installed userscripts for one browser (name, size)."""
    return _json_result(
        ctx.request_context.lifespan_context.browser.list_userscripts(browser_id)
    )


@mcp_server.tool()
@odda_tool
async def userscript_remove(
    browser_id: str, name: str, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Remove a userscript; it stops running on new navigations."""
    return await ctx.request_context.lifespan_context.browser.remove_userscript(
        browser_id, name
    )


# --- dynamic analysis: proxy-scripts ---


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


@mcp_server.tool(structured_output=False)
@odda_tool
async def proxy_script_list(*, ctx: Context[OddaState]) -> list[dict[str, Any]]:
    """List installed proxy-scripts (from disk, annotated with live state)."""
    return _json_result(ctx.request_context.lifespan_context.proxy.list_scripts())


@mcp_server.tool()
@odda_tool
async def proxy_script_remove(name: str, *, ctx: Context[OddaState]) -> dict[str, Any]:
    """Remove a proxy-script: live instance + on-disk source."""
    return ctx.request_context.lifespan_context.proxy.remove_script(name)


def main() -> None:
    """``odda mcp`` entrypoint: serve stdio JSON-RPC."""
    mcp_server.run()


if __name__ == "__main__":
    main()
