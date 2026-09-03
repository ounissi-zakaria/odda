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
  ``ToolParamError``, and the request/userscript/proxy-script
  libraries' ``ValueError``s to ``ToolError`` (message verbatim);
  anything else stays an SDK crash logged to stderr.
"""

from __future__ import annotations

import functools
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import resources as importlib_resources
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
from mcp.server.mcpserver.resources import TextResource

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
        for browser_id in [b["browser_id"] for b in browser.list_instances()]:
            await browser.close_instance(browser_id)
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
        "odda drives Chrome and captures HTTP traffic through a MITM "
        "proxy: open a browser, navigate, interact with pages by "
        "snapshot+ref, and read captured flows from .odda/flows/. "
        "Tools take explicit browser_id/tab_id ids from browser_open "
        "and tabs_open. Read the odda:// resources (request-crafting, "
        "flows, dynamic-analysis, userscripts, proxy-scripts, recipes) "
        "for the concept references before first use."
    ),
)


# --- resources: concept docs (odda://<slug>) ---


def _register_doc_resources() -> None:
    """Register the six static ``odda://`` markdown resources.

    The concept references agents read for orientation: request
    crafting, flow capture, dynamic analysis, userscripts,
    proxy-scripts, and worked recipes. Content lives in the
    ``odda.docs`` package (``src/odda/docs/``); read at registration
    time — these are server-instructions-scale documents, not lazily
    generated state.
    """
    docs = {
        "request-crafting": "Craft and send raw HTTP requests byte-for-byte.",
        "flows": "Flow file layout and the flows.jsonl schema.",
        "dynamic-analysis": "Wrap, logpoint, and coverage observation of JS execution.",
        "userscripts": (
            "Userscripts that auto-run at document_start; built-in dialog interceptor."
        ),
        "proxy-scripts": (
            "mitmproxy addons at the proxy layer; format, scope, failure model."
        ),
        "recipes": "Worked examples composing the tools into full investigations.",
    }
    for slug, description in docs.items():
        text = (
            importlib_resources.files("odda.docs")
            .joinpath(f"{slug}.md")
            .read_text(encoding="utf-8")
        )
        mcp_server.add_resource(
            TextResource(
                uri=f"odda://{slug}",
                name=slug,
                description=description,
                mime_type="text/markdown",
                text=text,
            )
        )


_register_doc_resources()


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
    other tool; the initial tab is ready immediately. Headless by
    default (headless=False shows the window for debugging or
    interactive use). All browser traffic routes through odda's HTTP
    proxy and is captured as flows under .odda/flows/ — driving the
    browser IS traffic capture; read flows via the odda://flows
    resource.
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
    """Close a tab in a browser.

    Closing the last tab leaves the browser open with zero tabs
    (matching Chrome's behavior); the browser can still accept
    tabs_open later. Close the whole browser with browser_close.
    """
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

    To open a tab, use tabs_open. wait_until: one of commit,
    domcontentloaded, load, networkidle (default load; pick
    domcontentloaded for SPAs whose load event never fires). timeout:
    page.goto timeout in seconds (default 30). Navigation failures
    (network error, invalid URL, lifecycle-event timeout) error; the
    timeout error names the wait_until event that failed.
    """
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


@mcp_server.tool()
@odda_tool
async def eval(
    browser_id: int,
    tab_id: int,
    js: str,
    *,
    ctx: Context[OddaState],
) -> Any:
    """Execute JavaScript in the target tab; return the raw value.

    Pass an expression, not a return statement (return is illegal at
    the top level — use an IIFE (()=>{ ... })() if you need
    statements). Returned Promises are awaited automatically:
    fetch(url).then(r => r.status) returns 200. Return a serializable
    value from async expressions — bare fetch(url) returns {} because
    the resolved Response is not JSON-serializable; chain
    .then(r => r.text()) or similar. eval has no timeout — it runs
    until the JS expression resolves, so a hung expression blocks the
    call indefinitely; wrap long enumeration loops in a bounded
    Promise.race if you need a deadline.
    """
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
    """Poll a JS expression until it's truthy or timeout in the target tab.

    Polling happens in-browser with no round-trips, in the main world
    (sees page globals and userscript-injected helpers). A thrown
    error inside the expression is treated as falsy and polling
    continues — document.querySelector('#root').children.length
    keeps polling while #root is still absent instead of crashing on
    the null deref. timeout: seconds (default 30).
    """
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

    The workflow: snapshot to discover element refs (eN, or f<frameSeq>eN
    inside an iframe), then pass a ref to page_click/page_fill/
    page_hover/page_upload; odda resolves it back to the element when
    the action runs. Refs stay valid while the element remains in the
    DOM — re-snapshot after a navigation or SPA swap; existing refs
    keep working without re-snapshotting. This is the ref-driven
    alternative to hand-written CSS selectors via eval, more robust on
    minified SPAs. Prefer this over screenshot for finding elements
    (text, cheap, carries refs); use screenshot for visual layout only.
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
            raise ToolParamError("Provide either x/y or ref, not both")
        if x is None or y is None:
            raise ToolParamError("Provide both x and y for coordinate mode")
        return await browser.page_click_coords(browser_id, tab_id, x=x, y=y)
    if ref is None:
        raise ToolParamError("Provide either ref or x/y coordinates")
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
    """Fill the element identified by ref with value.

    Clears the field first, then types. Works on text inputs,
    textareas, contenteditable elements, checkboxes ("true"/"false"),
    radios, and selects. The fill triggers input events (not change) —
    frameworks listening for change must be triggered otherwise.
    timeout: ref resolution + fill, seconds (default 5).
    """
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
    browser_id: int,
    tab_id: int,
    ref: str,
    files: list[str],
    timeout: float = 5.0,
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Upload local files to the file input identified by ref.

    Pass multiple paths in files for <input type="file" multiple>.
    This sets the files on the input but does NOT submit the form —
    click the form's submit button by ref separately to POST it. A
    nameless file input does not appear in the snapshot: eval an
    aria-label onto it, re-snapshot, then upload by the new ref (see
    odda://recipes). timeout: ref resolution + upload, seconds
    (default 5).
    """
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


# --- dynamic analysis: coverage ---


@mcp_server.tool()
@odda_tool
async def coverage_start(
    browser_id: int, tab_id: int, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Enable precise block-level coverage on the target tab.

    Per-tab; the recording window spans navigations (ADR-0005): flag,
    accumulator, and CDP Profiler all survive a navigate, so the
    workflow is start → navigate → snapshot/stop. Zero-hit blocks are
    included (the negative space is as informative as the positive).
    """
    return await ctx.request_context.lifespan_context.browser.coverage_start(
        browser_id, tab_id
    )


@mcp_server.tool()
@odda_tool
async def coverage_snapshot(
    browser_id: int, tab_id: int, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Read per-block hit counts on the target tab without stopping.

    Returns the delta since the previous take (or since start). Nested
    records: scripts → functions → ranges, each range a block with a
    hit count.
    """
    return await ctx.request_context.lifespan_context.browser.coverage_snapshot(
        browser_id, tab_id
    )


@mcp_server.tool()
@odda_tool
async def coverage_stop(
    browser_id: int, tab_id: int, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Take a final coverage snapshot and stop recording on the target tab.

    Returns the cumulative counts for the whole window (the sum of
    every take since start, including prior snapshots).
    """
    return await ctx.request_context.lifespan_context.browser.coverage_stop(
        browser_id, tab_id
    )


# --- dynamic analysis: wraps ---


@mcp_server.tool()
@odda_tool
async def wrap_calls_add(
    browser_id: int, tab_id: int, name: str, expr: str, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Install a call wrap on a named function.

    The wrap is a generated userscript: it takes effect on the next
    navigation (document_start), reaching all frames. Each call is
    recorded with receiver, arguments, return value, and call stack.
    Leaf-only (ADR-0003): callbacks passed as arguments are recorded
    as opaque refs, not themselves wrapped. Records wipe on navigation
    (ADR-0004); the installation persists (per browser, ADR-0010).
    """
    return await ctx.request_context.lifespan_context.browser.wrap_calls_add(
        browser_id, tab_id, name, expr
    )


@mcp_server.tool()
@odda_tool
async def wrap_access_add(
    browser_id: int, tab_id: int, name: str, expr: str, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Install an access wrap on a property accessor.

    Records every get/set of the property with the receiver, the
    value written (args[0] on a set), and the call stack. Same
    userscript-backed lifecycle as wrap_calls_add: effective next
    navigation, all frames, records wiped on navigation.
    """
    return await ctx.request_context.lifespan_context.browser.wrap_access_add(
        browser_id, tab_id, name, expr
    )


@mcp_server.tool()
@odda_tool
async def wrap_list(
    browser_id: int, tab_id: int, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """List installed wraps (name, type, expr) for the browser."""
    return await ctx.request_context.lifespan_context.browser.wrap_list(
        browser_id, tab_id
    )


@mcp_server.tool()
@odda_tool
async def wrap_remove(
    browser_id: int, tab_id: int, name: str, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Remove a wrap's userscript and reload the extension.

    The wrap stops recording on future navigations; already-recorded
    entries in the dump array are unaffected.
    """
    return await ctx.request_context.lifespan_context.browser.wrap_remove(
        browser_id, tab_id, name
    )


@mcp_server.tool()
@odda_tool
async def wrap_dump(
    browser_id: int, tab_id: int, name: str | None = None, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """Read the per-tab wrap record array.

    Each record: wrap name, type (call/access), args, ret, this,
    stack. Function values in args/ret are opaque refs {type:
    "function", name, source}; large or cyclic values are truncated.
    Use name to filter to one wrap's records.
    """
    return await ctx.request_context.lifespan_context.browser.wrap_dump(
        browser_id, tab_id, name
    )


@mcp_server.tool()
@odda_tool
async def wrap_clear(
    browser_id: int, tab_id: int, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Zero the per-tab wrap record array without navigating."""
    return await ctx.request_context.lifespan_context.browser.wrap_clear(
        browser_id, tab_id
    )


# --- dynamic analysis: logpoints ---


@mcp_server.tool()
@odda_tool
async def logpoint_add(  # noqa: PLR0913 — targeting + url/line/col/expr is the tool's contract
    browser_id: int,
    tab_id: int,
    url: str,
    line: int,
    col: int,
    expr: str,
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Plant a non-pausing logpoint at a source location.

    A CDP breakpoint evaluates expr in the paused-then-immediately-
    resumed frame's scope at each hit and records the result; the
    page never pauses. line and col are 0-based (minified code packs
    many statements per line, so the column picks the statement).
    Persists until explicitly removed; re-binds on navigation; does
    not survive tab close (per-tab-session, ADR-0004).
    """
    return await ctx.request_context.lifespan_context.browser.logpoint_add(
        browser_id, tab_id, url, line, col, expr
    )


@mcp_server.tool()
@odda_tool
async def logpoint_list(
    browser_id: int, tab_id: int, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """List planted logpoints on the target tab."""
    return await ctx.request_context.lifespan_context.browser.logpoint_list(
        browser_id, tab_id
    )


@mcp_server.tool()
@odda_tool
async def logpoint_remove(
    browser_id: int, tab_id: int, lp_id: str, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Remove a logpoint's CDP breakpoint and registry entry."""
    return await ctx.request_context.lifespan_context.browser.logpoint_remove(
        browser_id, tab_id, lp_id
    )


@mcp_server.tool()
@odda_tool
async def logpoint_dump(
    browser_id: int, tab_id: int, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """Read the per-tab logpoint record array.

    Each record: logpoint id, url, line, col, value (or null when the
    expression threw), error (null on success). Records accumulate
    across hits within one page load and wipe on navigation.
    """
    return await ctx.request_context.lifespan_context.browser.logpoint_dump(
        browser_id, tab_id
    )


@mcp_server.tool()
@odda_tool
async def logpoint_clear(
    browser_id: int, tab_id: int, *, ctx: Context[OddaState]
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
        path = Path(file)
        if not path.is_file():
            raise ToolParamError(f"File not found: {file}")
        return path.read_text(encoding="utf-8")
    return source  # type: ignore[return-value]


@mcp_server.tool()
@odda_tool
async def userscript_install(
    browser_id: int,
    name: str,
    file: str | None = None,
    source: str | None = None,
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Install a userscript into the given browser's scope.

    Runs at document_start in the main world on every page, before
    the page's own scripts; reaches all frames. Overwrites any
    existing userscript of the same name. The browser's extension
    reloads; already-loaded tabs are not re-injected (re-navigate to
    apply). Scope is per-browser (ADR-0010): a userscript on browser 1
    does not reach browser 2. Either file (read by the server) or
    inline source, not both.
    """
    js = _read_install_source(file, source, "js")
    if not js.strip():
        raise ToolParamError("source is empty")
    return await ctx.request_context.lifespan_context.browser.install_userscript(
        browser_id, name, js
    )


@mcp_server.tool()
@odda_tool
async def userscript_list(
    browser_id: int, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """List installed userscripts for one browser (name, size)."""
    return ctx.request_context.lifespan_context.browser.list_userscripts(browser_id)


@mcp_server.tool()
@odda_tool
async def userscript_remove(
    browser_id: int, name: str, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Remove a userscript from the given browser's scope and reload its extension."""
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
    """Install a proxy-script (mitmproxy addon) from a file or inline source.

    The source is a mitmproxy ``-s`` script: its module namespace is
    the addon (top-level request/response/load/running/... hooks, or
    an ``addons = [...]`` list). odda execs it in this server process,
    so it has full process privileges. Overwrite is gated by force.
    Persisted under ``<data_dir>/proxy-scripts/<name>/script.py`` and
    re-added on server boot. Scope is global: one proxy shared across
    all browsers (ADR-0018).
    """
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


def main() -> None:
    """``odda mcp`` entrypoint: serve stdio JSON-RPC."""
    mcp_server.run()


if __name__ == "__main__":
    main()
