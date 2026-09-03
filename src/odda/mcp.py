"""MCP server: stdio surface exposing odda's tools.

``odda mcp`` boots this module's :data:`mcp_server` as the *only* odda
process — the MCP lifespan replaces ``OddaServer`` as the owner of the
proxy, the browser manager, and the flowstore data dir. Tool handlers
are ports of today's ``OddaServer.method_*`` bodies; the result/error
contract is the one pinned in ticket #03 (``.scratch/odda-mcp/issues/
03-targeting-result-error-contract.md``):

- explicit required ``browser_id`` / ``tab_id`` params (no positional);
- pure natural return types — ``dict`` passes through, ``list``/``str``
  get the SDK's ``{"result": ...}`` wrap, ``Any`` is text-only;
- one ``@odda_tool`` decorator converts ``BrowserOperationError``,
  ``JsonRpcError``, and the request/userscript/proxy-script libraries'
  ``ValueError``s to ``ToolError`` (message verbatim, code discarded);
  anything else stays an SDK crash logged to stderr.
"""

from __future__ import annotations

import functools
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Context must be imported at runtime: the SDK resolves tool
# annotations via get_type_hints at add_tool time, so a
# TYPE_CHECKING-only import breaks Context-kwarg detection. TC002
# survives ``flake8-type-checking.runtime-evaluated-decorators`` here
# because ruff ignores that setting under PEP 563
# (``from __future__ import annotations``); verified against ruff 0.15.17.
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context  # noqa: TC002
from mcp.server.mcpserver.exceptions import ToolError

from odda import NAVIGATE_WAIT_UNTIL_EVENTS, __version__, flowstore, rpc
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

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def odda_tool(fn):
    """Convert odda's anticipated errors to SDK ``ToolError``.

    ``BrowserOperationError`` (library messages: tab not found, ref did
    not resolve, closed during …), ``JsonRpcError`` (handler validation:
    source is empty, ref-or-coords conflicts), and ``ValueError`` (the
    request/userscript/proxy-script libraries' anticipated validation:
    name collisions, not-found, empty request file, pre-emptive
    pipeline/repeat rejections — messages the old JSON-RPC dispatch
    surfaced via its catch-all) all become ``ToolError`` with the
    message verbatim — without this the model sees a content-free
    crash and loses every self-correction message. Everything else
    stays a crash: the SDK's ``UnexpectedToolError`` path, traceback
    to stderr, per the map's log decision.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except BrowserOperationError as exc:
            raise ToolError(str(exc)) from exc
        except rpc.JsonRpcError as exc:
            raise ToolError(str(exc)) from exc
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


@dataclass
class OddaState:
    """What the MCP lifespan owns — the trio ``OddaServer`` owned today.

    The data dir is not state here: ``flowstore.set_data_dir`` is a
    module-global setter, called once in the lifespan before any tool
    can run.
    """

    proxy: ProxyServer
    browser: BrowserManager


def resolve_data_dir() -> Path:
    """Resolve the data dir for this MCP process.

    ``ODDA_DATA_DIR`` env var (harness-injected today, dropped with the
    harness in ticket #07) wins; otherwise ``.odda/`` under the current
    working directory. The directory is remembered, not created — it
    appears lazily on first state write (ADR 0017).
    """
    return Path(os.environ.get("ODDA_DATA_DIR", Path(".odda").resolve())).resolve()


@asynccontextmanager
async def odda_lifespan(
    server: MCPServer[OddaState],  # noqa: ARG001 — SDK lifespan signature
) -> AsyncIterator[OddaState]:
    """Boot odda with the MCP process; tear down when the client leaves.

    Process start *is* server boot (ticket #02's verified seam): the
    same ownership order ``OddaServer.run`` uses — ``set_data_dir`` →
    ``ProxyServer()`` → ``BrowserManager(proxy=…)`` — minus the Unix
    socket, signal handlers, and parent watch that die with the CLI.
    Proxy-script restore on boot is deferred to ticket #12.
    """
    data_dir = resolve_data_dir()
    flowstore.set_data_dir(data_dir)
    proxy = ProxyServer()
    browser = BrowserManager(proxy=proxy)
    try:
        yield OddaState(proxy=proxy, browser=browser)
    finally:
        for browser_id in [b["browser_id"] for b in browser.list_instances()]:
            await browser.close_instance(browser_id)
        await proxy.shutdown()


mcp_server = MCPServer[OddaState](
    "odda",
    version=__version__,
    # description → serverInfo block in the initialize result: the short
    # "what is this server" label. instructions → initialize.instructions:
    # the detailed brief the agent reads at connect time.
    description="Browser automation and HTTP traffic capture for agents.",
    lifespan=odda_lifespan,
    # Full instructions are ticket #07's (docs-as-resources); the
    # orientation pointer stays brief per the map's docs-surface
    # decision (instructions = 2 lines, six concept docs as resources).
    instructions=(
        "odda: browser automation + HTTP capture for agents. "
        "Start with browser_open; read odda:// resources for concepts."
    ),
)


# --- browser lifecycle ---


@mcp_server.tool()
@odda_tool
async def browser_open(
    *,
    headless: bool = True,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Open a new Chrome browser window with one blank tab.

    Returns browser_id and the initial tab_id used to target every
    other tool. Headless by default.
    """
    return await ctx.request_context.lifespan_context.browser.open(headless=headless)


@mcp_server.tool()
@odda_tool
async def browser_close(browser_id: int, *, ctx: Context[OddaState]) -> dict[str, Any]:
    """Close a browser instance by id."""
    return await ctx.request_context.lifespan_context.browser.close_instance(browser_id)


@mcp_server.tool()
@odda_tool
async def browser_list(
    *,
    ctx: Context[OddaState],
) -> list[dict[str, Any]]:
    """List every tracked browser with its tab count."""
    return ctx.request_context.lifespan_context.browser.list_instances()


# --- tab lifecycle ---


@mcp_server.tool()
@odda_tool
async def tabs_list(
    browser_id: int | None = None, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """List open tabs, optionally filtered to one browser."""
    return await ctx.request_context.lifespan_context.browser.list_tabs(browser_id)


@mcp_server.tool()
@odda_tool
async def tabs_open(
    browser_id: int,
    url: str | None = None,
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Open a new tab in a browser, optionally navigating to a URL."""
    return await ctx.request_context.lifespan_context.browser.open_tab(browser_id, url)


@mcp_server.tool()
@odda_tool
async def tabs_close(
    browser_id: int, tab_id: int, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Close a tab in a browser."""
    return await ctx.request_context.lifespan_context.browser.close_tab(
        browser_id, tab_id
    )


# --- page control ---


@mcp_server.tool()
@odda_tool
async def navigate(
    browser_id: int,
    tab_id: int,
    url: str,
    timeout: float = 30.0,
    wait_until: str = "load",
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Navigate an existing tab to a URL.

    wait_until: one of commit, domcontentloaded, load, networkidle.
    timeout: page.goto timeout in seconds (default 30).
    """
    if wait_until not in NAVIGATE_WAIT_UNTIL_EVENTS:
        raise rpc.JsonRpcError(
            rpc.INVALID_PARAMS,
            f"wait_until must be one of {', '.join(NAVIGATE_WAIT_UNTIL_EVENTS)}, "
            f"got {wait_until!r}",
        )
    return await ctx.request_context.lifespan_context.browser.navigate(
        browser_id,
        tab_id,
        url,
        timeout_ms=timeout * 1000,
        wait_until=wait_until,
    )


@mcp_server.tool()
@odda_tool
async def eval(
    browser_id: int,
    tab_id: int,
    js: str,
    *,
    ctx: Context[OddaState],
) -> Any:
    """Execute JavaScript in the target tab; return the raw value."""
    return await ctx.request_context.lifespan_context.browser.eval_js(
        browser_id, tab_id, js
    )


@mcp_server.tool()
@odda_tool
async def wait_for(
    browser_id: int,
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


@mcp_server.tool()
@odda_tool
async def screenshot(
    browser_id: int,
    tab_id: int,
    output: str | None = None,
    *,
    ctx: Context[OddaState],
) -> str:
    """Capture a JPEG of the target tab's viewport.

    output: optional path to write the JPEG to; a temp file is
    generated when omitted. Returns the written path.
    """
    return await ctx.request_context.lifespan_context.browser.screenshot(
        browser_id, tab_id, output
    )


# --- page interaction (snapshot + ref-driven actions) ---


@mcp_server.tool()
@odda_tool
async def page_snapshot(
    browser_id: int, tab_id: int, *, ctx: Context[OddaState]
) -> str:
    """Take an agent-readable snapshot of the page's accessibility tree.

    Element refs (e.g. e2, f1e2) in the snapshot target page_click,
    page_fill, page_hover, and page_upload.
    """
    return await ctx.request_context.lifespan_context.browser.page_snapshot(
        browser_id, tab_id
    )


@mcp_server.tool()
@odda_tool
async def page_click(  # noqa: PLR0913 — targeting + ref/coords union is the tool's contract
    browser_id: int,
    tab_id: int,
    ref: str | None = None,
    x: float | None = None,
    y: float | None = None,
    timeout: float = 5.0,
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Click the element identified by ref, or at viewport coordinates.

    Provide either ref (element click, waits for actionability) or both
    x and y (raw trusted click); the two are mutually exclusive.
    timeout: ref mode only, seconds (default 5).
    """
    browser = ctx.request_context.lifespan_context.browser
    if x is not None or y is not None:
        if ref is not None:
            raise rpc.JsonRpcError(
                rpc.INVALID_PARAMS, "Provide either x/y or ref, not both"
            )
        if x is None or y is None:
            raise rpc.JsonRpcError(
                rpc.INVALID_PARAMS, "Provide both x and y for coordinate mode"
            )
        return await browser.page_click_coords(browser_id, tab_id, x=x, y=y)
    if ref is None:
        raise rpc.JsonRpcError(
            rpc.INVALID_PARAMS, "Provide either ref or x/y coordinates"
        )
    return await browser.page_click(browser_id, tab_id, ref, timeout_ms=timeout * 1000)


@mcp_server.tool()
@odda_tool
async def page_fill(
    browser_id: int,
    tab_id: int,
    ref: str,
    value: str,
    timeout: float = 5.0,
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Fill the element identified by ref with value."""
    return await ctx.request_context.lifespan_context.browser.page_fill(
        browser_id, tab_id, ref, value, timeout_ms=timeout * 1000
    )


@mcp_server.tool()
@odda_tool
async def page_hover(  # noqa: PLR0913 — targeting + ref/coords union is the tool's contract
    browser_id: int,
    tab_id: int,
    ref: str | None = None,
    x: float | None = None,
    y: float | None = None,
    timeout: float = 5.0,
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Hover the element identified by ref, or at viewport coordinates.

    Provide either ref or both x and y; mutually exclusive.
    timeout: ref mode only, seconds (default 5).
    """
    browser = ctx.request_context.lifespan_context.browser
    if x is not None or y is not None:
        if ref is not None:
            raise rpc.JsonRpcError(
                rpc.INVALID_PARAMS, "Provide either x/y or ref, not both"
            )
        if x is None or y is None:
            raise rpc.JsonRpcError(
                rpc.INVALID_PARAMS, "Provide both x and y for coordinate mode"
            )
        return await browser.page_hover_coords(browser_id, tab_id, x=x, y=y)
    if ref is None:
        raise rpc.JsonRpcError(
            rpc.INVALID_PARAMS, "Provide either ref or x/y coordinates"
        )
    return await browser.page_hover(browser_id, tab_id, ref, timeout_ms=timeout * 1000)


@mcp_server.tool()
@odda_tool
async def page_upload(
    browser_id: int,
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


@mcp_server.tool()
@odda_tool
async def event_listeners(
    browser_id: int, tab_id: int, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """List JavaScript event listeners on window and document in the target tab."""
    return await ctx.request_context.lifespan_context.browser.list_event_listeners(
        browser_id, tab_id
    )


@mcp_server.tool()
@odda_tool
async def status(*, ctx: Context[OddaState]) -> dict[str, Any]:
    """Show odda status: data dir, proxy URL, browser count."""
    state = ctx.request_context.lifespan_context
    return {
        "data_dir": str(resolve_data_dir()),
        "proxy_url": state.proxy.proxy_url,
        "browser_count": state.browser.browser_count,
    }


@mcp_server.tool()
@odda_tool
async def version() -> dict[str, Any]:
    """Return the odda version (no state needed)."""
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
    """Clone a captured flow's request into an editable request.

    Copies the flow's request file and meta sidecar into the editable
    requests dir under ``name``. The stored request becomes editable;
    the Host header stays verbatim (may intentionally differ from the
    TCP destination for vhost/host-header tests).
    """
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
    r"""Create a new empty editable request.

    The agent edits ``<data_dir>/requests/<name>/request`` on disk
    (raw HTTP bytes — the file is the wire bytes for HTTP/1.1), then
    sends it with request_send.

    Args:
        name: Editable request name.
        host: Target host (TCP destination; the Host header in the
            request file goes on the wire verbatim and may differ).
        protocol: ``http`` or ``https`` (default ``https``).
        port: Target port (default 80 for http, 443 for https).
        line_terminator: Byte sequence the request parser splits header
            lines on, as a list of byte ints (default ``\r\n``). H2-only:
            for H2→H1 downgrade smuggling where a literal CRLF must live
            inside an H2 header value (e.g. ``:path``), set to ``\n``
            (``[10]``) so the parser splits on LF, preserving CR in
            values. Ignored for HTTP/1.1 request files (wire-faithful).
        force: Overwrite an existing request of the same name.
        ctx: SDK request context (unused; the requests dir is derived
            from the data dir the lifespan set).
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

    Three modes, exactly as the CLI's ``--name`` arity selected:

    - single (``name``): frozen single-shot — one request, one flow
      record (a dict). Errors are recorded as error flows (status_code
      null + error message), not tool errors.
    - pipeline (``names``, two or more): one HTTP/1.1 connection
      (sequential keep-alive, or ``pipelining`` for send-all-then-read-
      all) for smuggling response-queue poisoning / victim consumption;
      or all-HTTP/2 for concurrent stream-multiplex (multi-endpoint
      race). Returns a list of flow records in send order.
    - repeat (``repeat`` >= 2 with single ``name``): N concurrent
      copies — the race / limit-overrun path (H2 stream-multiplex with
      last-byte single-packet, H1 parallel connections). Returns a
      list, one flow per copy.

    See ADR-0019/0020/0021 (docs/adr/) for the semantics this port
    honors verbatim: two-phase durability, mid-sequence abort policy,
    per-stream error isolation, the one-request-one-response flow
    model, and the H2-only line-terminator constraint.
    """
    if repeat is not None and names is not None:
        raise ToolError(
            "--repeat is single-name only; use --repeat with one --name, "
            "or multi-name without --repeat (the combo is ambiguous)"
        )
    if repeat is not None and pipelining:
        raise ToolError(
            "--repeat is concurrent; --pipelining is H1 multi-name "
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


def main() -> None:
    """``odda mcp`` entrypoint: serve stdio JSON-RPC."""
    mcp_server.run()


if __name__ == "__main__":
    main()
