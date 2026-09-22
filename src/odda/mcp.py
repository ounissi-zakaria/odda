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
from importlib import resources as importlib_resources
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
from mcp.server.mcpserver.resources import TextResource
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
        "Browser automation, HTTP traffic capture, dynamic analysis, "
        "and raw request crafting. Use when driving Chrome, capturing "
        "flows, observing JS execution, or hand-building HTTP "
        "requests.\n"
        "All traffic that goes through proxy (including browser "
        "traffic) is captured as flows under .odda/flows/. "
        "Concept references: odda://docs/* resources."
    ),
)


# --- resources: concept docs (odda://docs/<slug>) ---


def _register_doc_resources() -> None:
    """Register the six static ``odda://docs/<slug>`` markdown resources.

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
            "Userscripts that auto-run at document_start; shipped default helper."
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
                uri=f"odda://docs/{slug}",
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
    browser_id: str | None = None,
    headless: bool = True,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Open a browser: a fresh one, or reopen a closed browser record.

    Without browser_id, launches a new browser under a fresh five-letter
    token (unique for the data dir's lifetime), its Chrome profile seeded
    once from the global Base profile. With browser_id, reopens that
    closed record: same token, same profile and userscripts — logins and
    site data come back, tabs start fresh. Ids are case-insensitive.
    Refuses an id that is already open (here or in another session) or
    unknown. All browser traffic is captured as flows under .odda/flows/
    — driving the browser is traffic capture (see odda://docs/flows).
    """
    return await ctx.request_context.lifespan_context.browser.open(
        browser_id=browser_id, headless=headless
    )


@mcp_server.tool()
@odda_tool
async def browser_close(browser_id: str, *, ctx: Context[OddaState]) -> dict[str, Any]:
    """Close a browser instance by id.

    Kills the Chrome process; the browser's record — its profile and
    userscripts under .odda/browsers/<id>/ — persists and can be
    reopened later with browser_open(browser_id=...).
    """
    return await ctx.request_context.lifespan_context.browser.close_instance(browser_id)


@mcp_server.tool(structured_output=False)
@odda_tool
async def browser_list(
    *,
    ctx: Context[OddaState],
) -> list[dict[str, Any]]:
    """List every browser record in the data dir, alphabetically.

    Rows carry browser_id and state: "open" (running here, with
    tab_count), "open in another session" (another MCP session on this
    project has it — it cannot be opened or driven from here), or
    "closed" (no tab_count; reopenable via browser_open(browser_id=...)).
    """
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
    """Open a new tab in a browser, optionally navigating to a URL.

    If the URL's page opens a dialog while loading (e.g. an on-load
    alert), the result carries the dialog's details instead of the
    open status; resolve it with dialog_handle.
    """
    return await ctx.request_context.lifespan_context.browser.open_tab(browser_id, url)


@mcp_server.tool()
@odda_tool
async def tabs_close(
    browser_id: str, tab_id: int, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Close a tab in a browser.

    Closing the last tab leaves the browser open with zero tabs;
    use browser_close to close the browser itself.
    """
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
    """Accept or dismiss the tab's open alert/confirm/prompt dialog.

    Native dialogs (alert/confirm/prompt/beforeunload) block the page
    until handled. accept answers OK/true (confirm) or
    the given prompt_text (prompt; supplies the answer, defaults to
    the dialog's default_value; ignored for non-prompts and with
    dismiss); dismiss answers cancel/false/null. While a dialog is
    open, same-tab tools reject with an error instead of running —
    handle the dialog first, then retry them. beforeunload
    fires on navigate away (accept completes it, dismiss stays) but
    never on tab/browser close.
    """
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
    """Navigate an existing tab to a URL (to open a new tab, use tabs_open).

    wait_until: one of commit, domcontentloaded, load, networkidle —
    pick domcontentloaded for SPAs whose load event never fires. On
    timeout the error names the wait_until event that failed.
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
    """Execute JavaScript in the target tab; return the raw value.

    Pass an expression, not a return statement — use an IIFE
    (() => { ... })() for statements. Returned Promises are awaited
    (fetch(url).then(r => r.status) returns 200); return a
    serializable value — bare fetch(url) returns {} (Response isn't
    JSON-serializable; chain .then(r => r.text())). No timeout: a
    hung expression blocks the call — wrap long loops in a bounded
    Promise.race. Pass the code inline as js, or a server-side
    path in file (mutually exclusive).
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
    """Poll a JS expression until it's truthy or timeout in the target tab.

    Polls in-browser in the main world. A thrown error counts as
    falsy and polling continues — document.querySelector('#root')
    .children.length keeps polling while #root is absent instead of
    crashing on the null deref.
    """
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
    """Capture a JPEG of the target tab's viewport.

    output: optional path to write the JPEG to (a temp file when
    omitted). The written path is always the first text block.
    return_image: inline the JPEG as an image content block after the
    text blocks (default) — a vision-capable harness sees the
    pixels without re-reading the file; a text-only harness collapses
    the image block to a placeholder while the path survives for
    on-demand reads. false returns the path (plus the legend when
    annotating) without an image block.
    annotate: draw a numbered marker on every Ref's bounding box and
    add a legend text block mapping each marker to its ref and
    viewport box (CSV: n,ref,x,y,w,h) — the visual counterpart of
    page_snapshot's boxes=true; pairs with coordinate clicks (canvas,
    overlays, elements the a11y tree can't name). Markers are drawn
    onto the captured pixels client-side; nothing is injected into
    the page. The legend sits between the path and the image block
    and is omitted when the page has no refs.
    """
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
    """Take an agent-readable snapshot of the page's accessibility tree.

    Snapshot to discover element refs (eN; f<frameSeq>eN inside an
    iframe), then pass a ref to page_click/page_fill/page_hover/
    page_upload. Refs stay valid until their element leaves the DOM —
    every snapshot covers the whole page, so no call invalidates
    earlier refs; re-snapshot after a navigation. Prefer this over
    screenshot for finding elements; use screenshot for visual layout.
    depth: cap tree depth; boundary nodes render without children.
    When the tree actually reaches the cap, each boundary line is
    tagged with its hidden subtree depth ([deeper=k]) and the result
    ends with a note stating the tree's real depth and how many lines
    are hidden (raise depth, or page_find, to see them); a tree that
    fits under the cap is returned without any such annotation.
    boxes: include [box=x,y,width,height] per line — geometry source
    for page_click/page_hover coordinates. screenshot(annotate=true)
    draws these boxes and labels onto the captured pixels instead.
    diff: return only what changed since this tab's previous same-
    depth snapshot — "-"/"+" lines (fresh refs on "+", old render's
    on "-") under an ancestor-path header per hunk; unchanged content
    is never re-sent. Comparison ignores refs, boxes, and cap tags,
    so scrolling never reads as change. Every page_snapshot becomes
    the baseline for its depth (diffs chain); navigation clears the
    tab's baselines; nothing changed returns the sentinel
    "(no changes since previous snapshot)"; a first diff returns the
    full tree, announced by a note. Prefer diff over a full re-read
    after every action.
    On a large page, prefer page_find (search without the full tree).
    """
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
    """Search the page's accessibility snapshot for a regex.

    Returns matching regions with context, not the whole tree.

    Cheaper than page_snapshot when you only need to locate an element
    and its ref on a large page: each match comes back with a few lines
    of context and its ancestor path from the tree root, so refs arrive
    with their location. The snapshot is taken fresh on every call.
    regex: Python re pattern, matched per line (case-sensitive; add
    (?i) for case-insensitive). Refs in results work in page_click/
    page_fill/page_hover/page_upload.
    boxes: include [box=x,y,width,height] per line — geometry source
    for page_click/page_hover coordinates. screenshot(annotate=true)
    draws these boxes and labels onto the captured pixels instead.
    """
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
    """Click the element identified by ref, or at viewport coordinates.

    Either ref (waits for the element to be actionable) or both x
    and y (raw trusted click), never both. timeout applies to ref
    mode only.
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
    """Fill the element identified by ref with value.

    Clears the field first. Works on text inputs, textareas,
    contenteditable, checkboxes ("true"/"false"), radios, and
    selects. Fires input events, not change — trigger change
    listeners separately. Pass value inline, or a server-side path
    in file preserving newlines (mutually exclusive).
    """
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
    """Hover the element identified by ref, or at viewport coordinates.

    Either ref or both x and y, never both. timeout applies to ref
    mode only.
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
    browser_id: str,
    tab_id: int,
    ref: str,
    files: list[str],
    timeout: float = 5.0,
    *,
    ctx: Context[OddaState],
) -> dict[str, Any]:
    """Upload local files to the file input identified by ref.

    Does NOT submit the form — click the submit button separately.
    A nameless file input doesn't appear in the snapshot: eval an
    aria-label onto it, re-snapshot, then upload by the new ref
    (odda://docs/recipes).
    """
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
    """Clone a captured flow's request into an editable request.

    The copy lives at .odda/requests/<name>/request — edit it,
    then send with request_send.
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
    """Create a new empty editable request.

    Creates .odda/requests/<name>/request (raw HTTP bytes — the
    wire bytes for HTTP/1.1) plus a meta.json sidecar; fill the
    request file, then send with request_send. See
    odda://docs/request-crafting for the file format and the
    line_terminator option.
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

    Modes by argument: single name → one flow record (a dict);
    names (two or more) → all sent on one connection, a list of
    records in send order (H1: sequential keep-alive, or
    ``pipelining`` for send-all-then-read-all; H2: concurrent
    streams); repeat >= 2 with a single name → N concurrent
    copies, one flow each. Errors are recorded as error flows
    (status_code null + error message), not tool errors. Requests
    dial direct from this machine's own IP — they never traverse
    odda's proxy or a proxy_upstream chain. See
    odda://docs/request-crafting for the full semantics.
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

    Chrome still connects to odda's proxy; every request odda forwards
    goes via the upstream instead of direct (mitmproxy upstream mode).
    ``url`` is the upstream proxy, ``http://`` or ``https://`` only
    (no SOCKS); no credentials in the URL — Basic auth goes in
    ``auth`` as 'username:password'. Loopback targets cannot be
    reached through a remote upstream: clear it before driving
    127.0.0.1 targets. Crafted requests (request_send) always dial
    direct from this machine regardless. An unreachable or refusing
    upstream fails per-flow: plain-http targets get a 502 naming the
    upstream, https targets get a dead TLS handshake — check
    flow.error in flows.jsonl. Open connections are closed so the new
    path applies on the very next request — even over pooled HTTP/2
    (in-flight requests on them abort). Per-session.
    """
    return await ctx.request_context.lifespan_context.proxy.set_upstream(url, auth)


@mcp_server.tool()
@odda_tool
async def proxy_upstream_clear(*, ctx: Context[OddaState]) -> dict[str, Any]:
    """Remove the upstream proxy and return to direct egress.

    Open connections are closed so direct egress applies on the very
    next request (in-flight requests on them abort); browsers
    reconnect transparently. Session-scoped like proxy_upstream_set.
    """
    return await ctx.request_context.lifespan_context.proxy.clear_upstream()


@mcp_server.tool()
@odda_tool
async def proxy_upstream_get(*, ctx: Context[OddaState]) -> dict[str, Any]:
    """Report the current upstream proxy configuration.

    Returns ``upstream`` (the URL, or ``None`` when direct) and
    ``auth_set`` (whether credentials are configured; the secret is
    never echoed back).
    """
    return ctx.request_context.lifespan_context.proxy.upstream_state()


# --- dynamic analysis: coverage ---


@mcp_server.tool()
@odda_tool
async def coverage_start(
    browser_id: str, tab_id: int, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Enable block-level coverage on the target tab.

    Per-tab and spans navigations: the workflow is start →
    navigate → snapshot/stop. Zero-hit blocks are included.
    """
    return await ctx.request_context.lifespan_context.browser.coverage_start(
        browser_id, tab_id
    )


@mcp_server.tool()
@odda_tool
async def coverage_snapshot(
    browser_id: str, tab_id: int, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Read per-block hit counts on the target tab without stopping.

    Returns the delta since the previous take (or since start);
    records nest scripts → functions → ranges.
    """
    return await ctx.request_context.lifespan_context.browser.coverage_snapshot(
        browser_id, tab_id
    )


@mcp_server.tool()
@odda_tool
async def coverage_stop(
    browser_id: str, tab_id: int, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Take a final coverage snapshot and stop recording.

    Returns the cumulative counts for the whole window (the sum
    of every take since start, including prior snapshots).
    """
    return await ctx.request_context.lifespan_context.browser.coverage_stop(
        browser_id, tab_id
    )


# --- dynamic analysis: wraps ---


@mcp_server.tool()
@odda_tool
async def wrap_calls_add(
    browser_id: str, name: str, expr: str, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Install a call wrap on a named function.

    Takes effect on the next navigation (already-loaded tabs are
    not re-injected). Leaf-only: callbacks passed as arguments are
    not wrapped. Records wipe on navigation; the installation
    persists per browser.
    """
    return await ctx.request_context.lifespan_context.browser.wrap_calls_add(
        browser_id, name, expr
    )


@mcp_server.tool()
@odda_tool
async def wrap_access_add(
    browser_id: str, name: str, expr: str, *, ctx: Context[OddaState]
) -> dict[str, Any]:
    """Install an access wrap on a property accessor.

    Records every get/set with receiver, value, and stack. Same
    lifecycle as wrap_calls_add: effective next navigation,
    records wipe on navigation.
    """
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
    """Remove a wrap; it stops recording on future navigations.

    Already-recorded entries are unaffected.
    """
    return await ctx.request_context.lifespan_context.browser.wrap_remove(
        browser_id, name
    )


@mcp_server.tool(structured_output=False)
@odda_tool
async def wrap_dump(
    browser_id: str, tab_id: int, name: str | None = None, *, ctx: Context[OddaState]
) -> list[dict[str, Any]]:
    """Read the per-tab wrap record array.

    Each record: wrap name, type (call/access), this, args, ret,
    stack; functions and large/cyclic values are serialized
    compactly. Pass name to filter to one wrap's records.
    """
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
    """Plant a non-pausing logpoint at a source location.

    Each hit evaluates expr in the frame's scope (locals are
    readable) and records the result; the page never pauses. line
    and col are 0-based. Persists until removed, re-binds on
    navigation, dies with the tab.
    """
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
    """Read the per-tab logpoint record array.

    Each record: logpoint id, url, line, col, value (null when the
    expression threw), error. Wipes on navigation.
    """
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
    """Install a userscript into the given browser's scope.

    Runs at document_start in the main world before the page's
    own scripts, in every tab and frame of the browser. Scope is
    per-browser; a same-name install overwrites. Takes effect on
    new navigations (already-loaded tabs are not re-injected).
    Pass file or source, not both.
    """
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
    """Install a proxy-script (mitmproxy ``-s`` addon) from file or source.

    Runs with full server-process privileges. Overwriting an
    existing name requires force. Persisted and re-added on boot;
    scope is global (one proxy shared across all browsers). See
    odda://docs/proxy-scripts for the format.
    """
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
