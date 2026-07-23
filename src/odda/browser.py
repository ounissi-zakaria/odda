"""Browser automation module using patchright (Playwright)."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from patchright.async_api import (
    BrowserContext,
    CDPSession,
    Page,
    Playwright,
    async_playwright,
)

from odda import (
    coverage as coverage_mod,
    logpoint as logpoint_mod,
    userscript as userscript_mod,
    wrap as wrap_mod,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_BASE_PROFILE_DIR = Path.home() / ".config" / "odda" / "chrome-profile"


def _find_chrome_executable() -> str:
    """Find a system Chrome/Chromium executable.

    Returns:
        Path to the Chrome binary.

    Raises:
        RuntimeError: If no Chrome executable is found on PATH.
    """
    candidates = [
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ]
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    msg = "No Chrome or Chromium executable found on PATH"
    raise RuntimeError(msg)


def _prepare_user_data_dir() -> str:
    """Create a temp user data dir, optionally seeded from the base profile.

    Returns:
        Path to a temporary directory that can be used as Chrome's
        user data directory.
    """
    temp_dir = tempfile.mkdtemp(prefix="odda_")
    if _BASE_PROFILE_DIR.is_dir():
        with suppress(OSError):
            shutil.copytree(_BASE_PROFILE_DIR, temp_dir, dirs_exist_ok=True)
    return temp_dir


class BrowserOperationError(Exception):
    """Raised when a browser/tab operation fails for a targetable reason.

    Carries a short message that surfaces to the CLI as a JSON-RPC
    INVALID_PARAMS error. Used for unknown browser/tab ids, mismatched
    pairs, target-closed-during-op, goto failures, and screenshot
    failures.
    """

    def __init__(self, message: str) -> None:
        """Initialize with a short human-readable message."""
        super().__init__(message)
        self.message = message


def _is_target_closed_error(exc: BaseException) -> bool:
    """Return True if exc looks like a Playwright 'target closed' error."""
    msg = str(exc).lower()
    return "target closed" in msg or "has been closed" in msg


class BrowserInstance:
    """Manages a single Chrome browser context and its tabs.

    Every operation targets a tab_id returned by open_tab / list_tabs /
    the browser_open handler. tab_ids are monotonic per-instance and
    never reused.
    """

    def __init__(
        self,
        browser_id: int,
        playwright: Playwright,
        context: BrowserContext,
    ) -> None:
        """Initialize a browser instance.

        Args:
            browser_id: Unique ID for this browser.
            playwright: Patchright Playwright object.
            context: BrowserContext (persistent context for this browser).
        """
        self.browser_id = browser_id
        self.playwright = playwright
        self.context = context
        self._next_tab_id: int = 1
        self._tabs: dict[int, Page] = {}
        self._cdp_sessions: dict[int, CDPSession] = {}
        self._script_maps: dict[int, dict[str, str]] = {}
        self._coverage_recording: dict[int, bool] = {}
        self._coverage_accumulators: dict[int, dict[str, Any]] = {}
        # Per-tab logpoint state: ``{tab_id: [{id, cdp_id, url, line,
        # col, expr}]}``. The ``id`` is server-generated (``lp-<n>`` per
        # tab) and embedded in the breakpoint condition so records carry
        # it; ``cdp_id`` is the CDP ``breakpointId`` used for
        # ``Debugger.removeBreakpoint``. Per ADR-0004, logpoint
        # installations persist across navigation (the CDP breakpoint
        # re-binds to the re-loaded script) and do not survive tab close
        # (cleared in _on_page_close).
        self._logpoints: dict[int, list[dict[str, Any]]] = {}
        self._logpoint_counter: int = 0
        self._browser_cdp_session: CDPSession | None = None
        self._extension_id: str | None = None

        # New pages (from context.new_page or window.open) auto-register.
        self.context.on("page", self._on_context_page)

    # --- tab lifecycle -------------------------------------------------

    def _on_context_page(self, page: Page) -> None:
        """Register a page created by context.new_page() or window.open.

        This is the single registration point: open_tab and the initial-page
        path both let context.on('page') do the registration (Playwright
        fires it synchronously during new_page()), then look up the
        assigned tab_id by page identity.
        """
        if any(p is page for p in self._tabs.values()):
            return
        self._register_page(page)

    def _tab_id_for_page(self, page: Page) -> int:
        """Return the tab_id assigned to a Page, or raise."""
        for tab_id, p in self._tabs.items():
            if p is page:
                return tab_id
        raise BrowserOperationError(
            f"Page was not registered in browser {self.browser_id}."
        )

    def _register_page(self, page: Page) -> int:
        """Assign a tab_id to a page, wire listeners, schedule CDP setup.

        CDP setup (Debugger.enable + scriptParsed listener) is scheduled as
        an asyncio task so it runs on the event loop without blocking. It
        must complete before page scripts fire so that scriptParsed events
        populate the script_map by the time list_event_listeners is called.

        Returns:
            The new tab_id.
        """
        tab_id = self._next_tab_id
        self._next_tab_id += 1
        self._tabs[tab_id] = page
        self._script_maps[tab_id] = {}
        page.on("framenavigated", lambda frame: self._on_frame_navigated(tab_id, frame))
        page.on("close", lambda: self._on_page_close(tab_id))
        with suppress(RuntimeError):
            # No running loop: _get_cdp_session will lazily create it later.
            asyncio.get_running_loop().create_task(
                self._setup_cdp_session(tab_id, page)
            )
        return tab_id

    def _on_frame_navigated(self, tab_id: int, frame) -> None:
        """Clear that tab's script map on main-frame navigation.

        Per ADR-0005, Coverage is navigation-persistent: the per-tab
        recording flag, accumulator, and CDP Profiler domain all survive
        navigation so an agent can ``coverage start`` → ``navigate`` →
        ``snapshot``/``stop`` to observe code that runs as a consequence
        of navigating. Nothing coverage-related is torn down here.

        Logpoint installations persist across navigation (the CDP
        ``Debugger.setBreakpointByUrl`` re-binds to the re-loaded
        script), so the per-tab logpoint registry is NOT cleared here.
        Only the records (in ``window.__oddaLogpoint``) wipe on
        navigation — the default logpoint userscript re-initializes the
        array on each ``document_start``.
        """
        if frame.parent_frame is None:
            self._script_maps.get(tab_id, {}).clear()

    def _on_page_close(self, tab_id: int) -> None:
        """Tear down per-tab state when a page closes."""
        with suppress(Exception):
            cdp = self._cdp_sessions.pop(tab_id, None)
            if cdp is not None:
                cdp.detach()
        self._tabs.pop(tab_id, None)
        self._script_maps.pop(tab_id, None)
        self._coverage_recording.pop(tab_id, None)
        self._coverage_accumulators.pop(tab_id, None)
        # Per ADR-0004, logpoints do not survive tab close. The CDP
        # session is detached above so the breakpoints are gone; clear
        # the registry so `logpoint list` on a reopened tab is empty.
        self._logpoints.pop(tab_id, None)

    async def _setup_cdp_session(self, tab_id: int, page: Page) -> None:
        """Create and configure the per-tab CDP session (best-effort).

        Enables Debugger so scriptParsed events populate the script_map.
        Runtime, Console, and Page domains are intentionally left disabled
        to avoid detection leaks. Errors are logged but not raised: a tab
        whose CDP session can't be created still works for
        eval/wait_for/screenshot/navigate; only list_event_listeners will
        be unable to resolve script URLs (and _get_cdp_session will retry).
        """
        script_map = self._script_maps.setdefault(tab_id, {})

        def _on_script_parsed(event: dict[str, Any]) -> None:
            script_map[event.get("scriptId", "")] = event.get("url", "")

        try:
            cdp = await self.context.new_cdp_session(page)
            await cdp.send("Debugger.enable")
            cdp.on("Debugger.scriptParsed", _on_script_parsed)
            self._cdp_sessions[tab_id] = cdp
        except Exception as exc:
            logger.warning("Failed to set up CDP session for tab %s: %s", tab_id, exc)

    async def _get_cdp_session(self, tab_id: int) -> CDPSession:
        """Return the CDP session for a tab, creating it lazily if absent."""
        self._require_tab(tab_id)
        cdp = self._cdp_sessions.get(tab_id)
        if cdp is None:
            page = self._tabs[tab_id]
            await self._setup_cdp_session(tab_id, page)
            cdp = self._cdp_sessions.get(tab_id)
            if cdp is None:
                raise BrowserOperationError(
                    f"Failed to set up CDP session for tab {tab_id}."
                )
        return cdp

    def _require_tab(self, tab_id: int) -> Page:
        """Return the Page for tab_id or raise BrowserOperationError."""
        page = self._tabs.get(tab_id)
        if page is None or page.is_closed():
            self._tabs.pop(tab_id, None)
            raise BrowserOperationError(
                f"Tab {tab_id} not found in browser {self.browser_id}."
            )
        return page

    # --- userscript extension -----------------------------------------

    async def _load_userscript_extension(self) -> str | None:
        """Load the userscript extension into this browser via CDP.

        Creates a browser-level CDP session and calls
        ``Extensions.loadUnpacked``. The extension's content script runs at
        ``document_start`` in the MAIN world on every page.

        Returns:
            The extension ID, or None if loading failed.
        """
        if self._browser_cdp_session is None:
            browser = self.context.browser
            if browser is None:
                return None
            self._browser_cdp_session = await browser.new_browser_cdp_session()
        ext_path = str(userscript_mod.sync_extension(self.browser_id))
        try:
            resp = await self._browser_cdp_session.send(
                "Extensions.loadUnpacked", {"path": ext_path}
            )
        except Exception as exc:
            logger.warning("Failed to load userscript extension: %s", exc)
            return None
        ext_id = resp.get("id")
        self._extension_id = ext_id
        return ext_id

    async def _reload_userscript_extension(self) -> str | None:
        """Reload the userscript extension: uninstall old, load new.

        Returns:
            The new extension ID, or None if loading failed.
        """
        if self._browser_cdp_session is not None and self._extension_id:
            with suppress(Exception):
                await self._browser_cdp_session.send(
                    "Extensions.uninstall", {"id": self._extension_id}
                )
            self._extension_id = None
        return await self._load_userscript_extension()

    # --- operations ----------------------------------------------------

    async def list_tabs(self) -> list[dict]:
        """List all open tabs in this browser context."""
        tabs = []
        for tab_id, page in self._tabs.items():
            if page.is_closed():
                continue
            title = ""
            with suppress(Exception):
                title = await page.title()
            tabs.append(
                {
                    "tab_id": tab_id,
                    "url": page.url,
                    "title": title,
                }
            )
        return tabs

    async def open_tab(self, url: str | None = None) -> int:
        """Open a new tab, optionally navigating to ``url``.

        context.on('page') registers the new page synchronously during
        new_page(); we look up the assigned tab_id by identity.

        Returns:
            The new tab_id.
        """
        page = await self.context.new_page()
        tab_id = self._tab_id_for_page(page)
        if url is not None:
            await page.goto(url)
        return tab_id

    async def close_tab(self, tab_id: int) -> None:
        """Close a tab by id. No-op if already closed."""
        page = self._require_tab(tab_id)
        with suppress(Exception):
            await page.close()
        self._on_page_close(tab_id)

    async def navigate(self, tab_id: int, url: str) -> None:
        """Navigate an existing tab to ``url``."""
        page = self._require_tab(tab_id)
        try:
            await page.goto(url)
        except Exception as exc:
            if _is_target_closed_error(exc):
                self._on_page_close(tab_id)
                raise BrowserOperationError(
                    f"Tab {tab_id} closed during navigation."
                ) from exc
            msg = str(exc)
            if "timeout" in msg.lower() or "Timeout" in type(exc).__name__:
                hint = (
                    " For SPAs that don't fire `load`, use `odda wait-for"
                    ' "<expr>"` after navigate to poll for a condition.'
                )
                raise BrowserOperationError(f"Failed to navigate: {msg}{hint}") from exc
            raise BrowserOperationError(f"Failed to navigate: {msg}") from exc

    async def eval_js(self, tab_id: int, js_code: str) -> Any:
        """Execute JavaScript in the target tab's main world."""
        page = self._require_tab(tab_id)
        try:
            return await page.evaluate(js_code, isolated_context=False)
        except Exception as exc:
            if _is_target_closed_error(exc):
                self._on_page_close(tab_id)
                raise BrowserOperationError(
                    f"Tab {tab_id} closed during eval."
                ) from exc
            return f"JavaScript error: {exc!s}"

    async def wait_for(self, tab_id: int, expression: str, *, timeout_ms: float) -> Any:
        """Poll a JS expression until truthy or timeout in the target tab."""
        page = self._require_tab(tab_id)
        fn = f"() => {{ const v = ({expression}); return v ? v : false; }}"
        try:
            handle = await page.wait_for_function(fn, timeout=timeout_ms)
        except Exception as exc:
            if _is_target_closed_error(exc):
                self._on_page_close(tab_id)
                raise BrowserOperationError(
                    f"Tab {tab_id} closed during wait-for."
                ) from exc
            raise
        try:
            return await handle.json_value()
        except Exception:
            return str(handle)

    async def screenshot(self, tab_id: int, output_path: str | None = None) -> str:
        """Capture a JPEG screenshot of the target tab's viewport.

        Args:
            tab_id: Target tab.
            output_path: Optional path to write the JPEG to. When None,
                a temp file path under the system temp dir is generated
                (legacy behavior). When given, the directory must exist.
        """
        page = self._require_tab(tab_id)
        try:
            if output_path is not None:
                out = Path(output_path)
                out.parent.mkdir(parents=True, exist_ok=True)
                target = out
            else:
                temp_dir = Path(tempfile.gettempdir())
                target = temp_dir / f"screenshot_{int(time.time())}.jpeg"
            await page.screenshot(
                path=str(target),
                type="jpeg",
                full_page=False,
            )
            return str(target)
        except Exception as exc:
            if _is_target_closed_error(exc):
                self._on_page_close(tab_id)
                raise BrowserOperationError(
                    f"Tab {tab_id} closed during screenshot."
                ) from exc
            raise BrowserOperationError(f"Screenshot error: {exc!s}") from exc

    # --- page interaction --------------------------------------------

    async def page_snapshot(self, tab_id: int) -> str:
        """Return the page's accessibility tree as agent-readable text.

        Calls Playwright's ``page.aria_snapshot(mode="ai")`` which returns
        a YAML-ish serialization of the a11y tree with ``[ref=eN]`` tags
        (or ``[ref=f<frameSeq>eN]`` inside iframes). The agent greps the
        text for the element it wants and passes the ref to ``click``,
        ``fill``, ``hover``, or ``upload``.
        """
        page = self._require_tab(tab_id)
        try:
            return await page.aria_snapshot(mode="ai")
        except Exception as exc:
            if _is_target_closed_error(exc):
                self._on_page_close(tab_id)
                raise BrowserOperationError(
                    f"Tab {tab_id} closed during snapshot."
                ) from exc
            raise BrowserOperationError(f"Snapshot error: {exc!s}") from exc

    async def _ref_action(
        self,
        tab_id: int,
        ref: str,
        verb: str,
        action: Callable[[], Awaitable[Any]],
        timeout_ms: float,
    ) -> None:
        """Run a ref-driven Playwright action with unified error handling.

        Runs ``action`` (which should call the Playwright Locator method
        with the desired timeout) and converts Playwright timeouts and
        target-closed errors into clean ``BrowserOperationError`` messages.
        A timeout may mean the ref is stale (element removed) or the
        element is present but not actionable (disabled, covered by an
        overlay); the error message reflects both possibilities.
        """
        try:
            await action()
        except Exception as exc:
            if _is_target_closed_error(exc):
                self._on_page_close(tab_id)
                raise BrowserOperationError(
                    f"Tab {tab_id} closed during {verb}."
                ) from exc
            if "timeout" in str(exc).lower() or "Timeout" in type(exc).__name__:
                raise BrowserOperationError(
                    f"ref {ref} did not resolve or become actionable "
                    f"within {timeout_ms}ms (the element may have been "
                    f"removed, or it may be disabled or covered by an "
                    f"overlay; take a new snapshot if stale)"
                ) from exc
            raise BrowserOperationError(f"{verb.capitalize()} error: {exc!s}") from exc

    async def page_click(
        self, tab_id: int, ref: str, *, timeout_ms: float
    ) -> dict[str, Any]:
        """Click the element identified by ``ref`` (plain left-click).

        Uses Playwright's ``aria-ref`` selector engine to resolve the ref
        to a Locator, then clicks it. If the ref no longer resolves
        (element removed, navigated away), the Playwright timeout is
        converted to a clean ``BrowserOperationError``.
        """
        page = self._require_tab(tab_id)
        locator = page.locator(f"aria-ref={ref}")
        await self._ref_action(
            tab_id,
            ref,
            "click",
            lambda: locator.click(timeout=timeout_ms),
            timeout_ms,
        )
        return {"status": "clicked", "ref": ref}

    async def page_fill(
        self, tab_id: int, ref: str, value: str, *, timeout_ms: float
    ) -> dict[str, Any]:
        """Fill the element identified by ``ref`` with ``value``.

        Clears the field first, then types the value (Playwright's
        ``locator.fill()``). Works on text inputs, textareas, contenteditable
        elements, checkboxes (``"true"``/``"false"``), radios, and selects.
        """
        page = self._require_tab(tab_id)
        locator = page.locator(f"aria-ref={ref}")
        await self._ref_action(
            tab_id,
            ref,
            "fill",
            lambda: locator.fill(value, timeout=timeout_ms),
            timeout_ms,
        )
        return {"status": "filled", "ref": ref}

    async def page_hover(
        self, tab_id: int, ref: str, *, timeout_ms: float
    ) -> dict[str, Any]:
        """Hover the element identified by ``ref``.

        Auto-scrolls the element into view (Playwright's actionability
        check) before dispatching the hover.
        """
        page = self._require_tab(tab_id)
        locator = page.locator(f"aria-ref={ref}")
        await self._ref_action(
            tab_id,
            ref,
            "hover",
            lambda: locator.hover(timeout=timeout_ms),
            timeout_ms,
        )
        return {"status": "hovered", "ref": ref}

    async def page_upload(
        self, tab_id: int, ref: str, files: list[str], *, timeout_ms: float
    ) -> dict[str, Any]:
        """Set files on a file input identified by ``ref``.

        Wraps Playwright's ``locator.set_input_files(files)``. Pass one
        path for a single file input, or multiple paths for
        ``<input type="file" multiple>``.
        """
        page = self._require_tab(tab_id)
        locator = page.locator(f"aria-ref={ref}")
        await self._ref_action(
            tab_id,
            ref,
            "upload",
            lambda: locator.set_input_files(files, timeout=timeout_ms),
            timeout_ms,
        )
        return {"status": "uploaded", "ref": ref, "files": files}

    async def list_event_listeners(self, tab_id: int) -> list[dict]:
        """List JavaScript event listeners on window and document.

        Uses DOMDebugger.getEventListeners so we get script IDs, line
        numbers, and column numbers. Only Debugger.enable is required;
        Runtime, Console, and Page domains are intentionally left
        disabled to avoid detection leaks.
        """
        page = self._require_tab(tab_id)
        if page.is_closed():
            raise BrowserOperationError(
                f"Tab {tab_id} not found in browser {self.browser_id}."
            )
        cdp = await self._get_cdp_session(tab_id)
        script_map = self._script_maps.get(tab_id, {})

        listeners: list[dict] = []
        for element_tag, expression in (("window", "window"), ("document", "document")):
            ref = await cdp.send(
                "Runtime.evaluate",
                {"expression": expression, "includeCommandLineAPI": True},
            )
            object_id = ref.get("result", {}).get("objectId")
            if not object_id:
                continue

            response = await cdp.send(
                "DOMDebugger.getEventListeners",
                {"objectId": object_id},
            )
            listeners.extend(
                {
                    "type": listener.get("type", ""),
                    "element_tag": element_tag,
                    "line_number": listener.get("lineNumber"),
                    "column_number": listener.get("columnNumber"),
                    "script_url": script_map.get(listener.get("scriptId")),
                }
                for listener in response.get("listeners", [])
            )

        return listeners

    # --- coverage ------------------------------------------------------

    async def coverage_start(self, tab_id: int) -> dict[str, Any]:
        """Enable precise block-level coverage on the tab.

        Marks the tab as recording and resets the per-tab accumulator.
        Per-tab: starting on one tab does not affect another. Calling
        ``start`` on a tab that is already recording is an error so the
        agent knows the previous recording is still live.

        Returns:
            ``{"status": "recording"}``.
        """
        self._require_tab(tab_id)
        if self._coverage_recording.get(tab_id):
            raise BrowserOperationError(f"Tab {tab_id} is already recording coverage.")
        cdp = await self._get_cdp_session(tab_id)
        await coverage_mod.start(cdp)
        self._coverage_accumulators[tab_id] = coverage_mod.new_accumulator()
        self._coverage_recording[tab_id] = True
        return {"status": "recording"}

    async def coverage_snapshot(self, tab_id: int) -> dict[str, Any]:
        """Read the delta since the last take without stopping.

        CDP ``Profiler.takePreciseCoverage`` resets its counters on each
        read, so this returns the delta since the previous take (or
        since ``start`` if this is the first take). The delta is also
        merged into the per-tab accumulator so ``coverage_stop`` can
        return the cumulative counts for the whole window. Zero-hit
        blocks are included. The recording flag stays set.

        Returns:
            The shaped coverage delta (see
            ``coverage._format_coverage``).
        """
        self._require_tab(tab_id)
        if not self._coverage_recording.get(tab_id):
            raise BrowserOperationError(f"Tab {tab_id} is not recording coverage.")
        cdp = await self._get_cdp_session(tab_id)
        script_map = self._script_maps.get(tab_id, {})
        delta = await coverage_mod.take_precise_coverage(cdp, script_map)
        accumulator = self._coverage_accumulators.get(tab_id)
        if accumulator is not None:
            coverage_mod.merge_delta(accumulator, delta)
        return delta

    async def coverage_stop(self, tab_id: int) -> dict[str, Any]:
        """Take a final delta and return the cumulative counts for the window.

        Takes one final delta, merges it into the per-tab accumulator,
        stops the CDP Profiler, clears the recording flag, and returns
        the accumulator formatted as the public coverage shape. The
        returned counts are the cumulative totals for the whole
        recording window (the sum of every take since ``start``,
        including any intermediate ``snapshot`` reads), so the agent
        gets the full-window picture regardless of whether they
        snapshotted mid-way. To slice a sub-window, subtract two
        ``snapshot`` deltas.

        Returns:
            The shaped cumulative coverage object for the recording
            window.
        """
        self._require_tab(tab_id)
        if not self._coverage_recording.get(tab_id):
            raise BrowserOperationError(f"Tab {tab_id} is not recording coverage.")
        cdp = await self._get_cdp_session(tab_id)
        script_map = self._script_maps.get(tab_id, {})
        delta = await coverage_mod.take_precise_coverage(cdp, script_map)
        accumulator = self._coverage_accumulators.pop(tab_id, None)
        if accumulator is None:
            accumulator = coverage_mod.new_accumulator()
        coverage_mod.merge_delta(accumulator, delta)
        await coverage_mod.stop(cdp)
        self._coverage_recording.pop(tab_id, None)
        return coverage_mod.format_accumulated(accumulator)

    # --- wrap ----------------------------------------------------------

    async def wrap_dump(
        self, tab_id: int, name: str | None = None
    ) -> list[dict[str, Any]]:
        """Read the per-tab wrap record array from ``window.__oddaWrap``.

        Args:
            tab_id: Target tab.
            name: Optional wrap name to filter to. The filter is applied
                server-side, after the full array is read from the page,
                so cross-origin iframe records (which live in their own
                per-iframe ``__oddaWrap``) are not filtered by this call.

        Returns:
            The list of wrap records (see the wrap module for the
            record shape), optionally filtered to one wrap's records.
        """
        self._require_tab(tab_id)
        result = await self.eval_js(
            tab_id,
            "typeof window.__oddaWrap === 'undefined' ? [] : window.__oddaWrap",
        )
        if not isinstance(result, list):
            return []
        if name is not None:
            return [r for r in result if isinstance(r, dict) and r.get("wrap") == name]
        return result

    async def wrap_clear(self, tab_id: int) -> dict[str, Any]:
        """Zero the per-tab wrap record array without navigating.

        Sets ``window.__oddaWrap = []`` in the tab. The wrap
        installations are unaffected; subsequent calls will continue
        to record.

        Returns:
            ``{"status": "cleared", "count": <records dropped>}``.
        """
        self._require_tab(tab_id)
        prev = await self.eval_js(
            tab_id,
            "typeof window.__oddaWrap === 'undefined' ? 0 : window.__oddaWrap.length",
        )
        await self.eval_js(tab_id, "window.__oddaWrap = []")
        return {"status": "cleared", "count": prev if isinstance(prev, int) else 0}

    # --- logpoint ------------------------------------------------------

    async def logpoint_add(
        self, tab_id: int, url: str, line: int, col: int, expr: str
    ) -> dict[str, Any]:
        """Plant a non-pausing logpoint at ``url:line:col``.

        Generates a per-tab logpoint id (``lp-<n>``), plants a CDP
        ``Debugger.setBreakpointByUrl`` with a non-pausing condition
        that evaluates ``expr`` in the paused frame's scope and pushes
        a record into ``window.__oddaLogpoint``, and registers the
        logpoint in the per-tab registry. The CDP logpoint persists
        across navigation (re-binds to the re-loaded script); the
        per-tab records wipe on navigation (the default logpoint
        userscript re-initializes the array on ``document_start``).

        Raises:
            BrowserOperationError: If a logpoint already exists at
                this ``(url, line, col)`` (CDP allows only one
                logpoint per location).

        Returns:
            ``{"status": "planted", "id", "cdp_id", "url",
            "line", "col", "expr"}`` and optionally ``"warning"`` if
            no loaded script matches ``url``.
        """
        self._require_tab(tab_id)
        cdp = await self._get_cdp_session(tab_id)
        script_map = self._script_maps.get(tab_id, {})
        registry = self._logpoints.setdefault(tab_id, [])
        existing = next(
            (
                lp
                for lp in registry
                if lp["url"] == url and lp["line"] == line and lp["col"] == col
            ),
            None,
        )
        if existing is not None:
            raise BrowserOperationError(
                f"A logpoint already exists at {url}:{line}:{col} "
                f"(id={existing['id']}); remove it first."
            )
        self._logpoint_counter += 1
        lp_id = f"lp-{self._logpoint_counter}"
        result = await logpoint_mod.add(cdp, lp_id, url, line, col, expr, script_map)
        registry.append(
            {
                "id": lp_id,
                "cdp_id": result.get("cdp_id", ""),
                "url": url,
                "line": line,
                "col": col,
                "expr": expr,
            }
        )
        return result

    async def logpoint_list(self, tab_id: int) -> list[dict[str, Any]]:
        """List planted logpoints for the tab.

        Returns:
            A list of ``{id, url, line, col, expr}`` dicts.
        """
        self._require_tab(tab_id)
        return [
            {
                "id": lp["id"],
                "url": lp["url"],
                "line": lp["line"],
                "col": lp["col"],
                "expr": lp["expr"],
            }
            for lp in self._logpoints.get(tab_id, [])
        ]

    async def logpoint_dump(self, tab_id: int) -> list[dict[str, Any]]:
        """Read the per-tab logpoint record array.

        The array is ``window.__oddaLogpoint``, initialized to ``[]``
        by the default logpoint userscript on each ``document_start``
        (per ADR-0004, records wipe on navigation). Each record is
        ``{logpoint, url, line, col, value, error}``.

        Returns:
            The list of logpoint records.
        """
        self._require_tab(tab_id)
        result = await self.eval_js(
            tab_id,
            "typeof window.__oddaLogpoint === 'undefined' ? [] : window.__oddaLogpoint",
        )
        if isinstance(result, list):
            return result
        return []

    async def logpoint_clear(self, tab_id: int) -> dict[str, Any]:
        """Zero the per-tab logpoint record array without navigating.

        Sets ``window.__oddaLogpoint = []``. The logpoint installations
        are unaffected; subsequent hits continue to record.

        Returns:
            ``{"status": "cleared", "count": <records dropped>}``.
        """
        self._require_tab(tab_id)
        prev = await self.eval_js(
            tab_id,
            "typeof window.__oddaLogpoint === 'undefined'"
            " ? 0 : window.__oddaLogpoint.length",
        )
        await self.eval_js(tab_id, "window.__oddaLogpoint = []")
        return {"status": "cleared", "count": prev if isinstance(prev, int) else 0}

    async def logpoint_remove(self, tab_id: int, lp_id: str) -> dict[str, Any]:
        """Remove a logpoint's CDP logpoint and registry entry.

        Args:
            tab_id: Target tab.
            lp_id: The logpoint id (``lp-<n>``) returned by
                :meth:`logpoint_add`.

        Raises:
            BrowserOperationError: If no logpoint with ``lp_id`` is
                registered on the tab.

        Returns:
            ``{"status": "removed", "id": lp_id}``.
        """
        self._require_tab(tab_id)
        registry = self._logpoints.get(tab_id, [])
        entry = next((lp for lp in registry if lp["id"] == lp_id), None)
        if entry is None:
            raise BrowserOperationError(f"Logpoint {lp_id} not found in tab {tab_id}.")
        cdp = await self._get_cdp_session(tab_id)
        cdp_id = entry.get("cdp_id", "")
        if cdp_id:
            with suppress(Exception):
                await logpoint_mod.remove(cdp, cdp_id)
        self._logpoints[tab_id] = [lp for lp in registry if lp["id"] != lp_id]
        return {"status": "removed", "id": lp_id}

    # --- teardown ------------------------------------------------------

    async def _close(self) -> None:
        """Close the browser context and release resources."""
        for tab_id in list(self._cdp_sessions.keys()):
            with suppress(Exception):
                await self._cdp_sessions[tab_id].detach()
        self._cdp_sessions.clear()
        self._tabs.clear()
        self._script_maps.clear()
        with suppress(Exception):
            if self.context:
                await self.context.close()
        with suppress(Exception):
            if self.playwright:
                await self.playwright.stop()
        self.context = None  # type: ignore[assignment]
        self.playwright = None  # type: ignore[assignment]


class BrowserManager:
    """Manages multiple Chrome browser instances via patchright."""

    def __init__(self, proxy=None):
        """Initialize browser manager.

        Args:
            proxy: Proxy server instance to share across browsers.
        """
        self._instances: dict[int, BrowserInstance] = {}
        self._next_id: int = 1
        self.proxy = proxy

    @property
    def browser_count(self) -> int:
        """Return the number of open browser instances."""
        return len(self._instances)

    def list_instances(self) -> list[dict]:
        """List open browser instances with tab counts (internal helper)."""
        return [
            {
                "browser_id": bid,
                "tab_count": len(inst._tabs),  # noqa: SLF001
            }
            for bid, inst in sorted(self._instances.items())
        ]

    def _require_instance(self, browser_id: int) -> BrowserInstance:
        """Return the BrowserInstance for browser_id or raise."""
        inst = self._instances.get(browser_id)
        if inst is None:
            raise BrowserOperationError(f"Browser {browser_id} not found.")
        return inst

    async def _create_instance(self, *, headless: bool = False) -> BrowserInstance:
        """Create and register a new BrowserInstance.

        Registers the initial page (the one Chrome creates at launch) and
        returns the instance with at least one tab_id assigned.
        """
        browser_id = self._next_id
        self._next_id += 1

        proxy_config = None
        if self.proxy:
            proxy_config = {"server": self.proxy.proxy_url}

        user_data_dir = _prepare_user_data_dir()
        chrome_executable = _find_chrome_executable()

        playwright = await async_playwright().start()
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            executable_path=chrome_executable,
            headless=headless,
            proxy=proxy_config,
            ignore_https_errors=True,
            ignore_default_args=[
                "--password-store=basic",
                "--use-mock-keychain",
            ],
            args=[
                "--no-first-run",
                "--no-default-browser-check",
                "--enable-unsafe-extension-debugging",
            ],
        )

        instance = BrowserInstance(browser_id, playwright, context)

        # Register the initial page (Chrome opens one automatically).
        # context.on('page') may have already fired for it during context
        # construction (before our handler was wired in __init__), so check
        # first and only register if it's not already tracked.
        initial_page = context.pages[0] if context.pages else await context.new_page()
        if not any(p is initial_page for p in instance._tabs.values()):  # noqa: SLF001
            instance._register_page(initial_page)  # noqa: SLF001

        # Load the userscript extension (best-effort).
        await instance._load_userscript_extension()  # noqa: SLF001

        # The initial page may have arrived before the context.on("page")
        # handler was wired, or it may re-fire; the handler guards against
        # double-registration by identity.
        self._instances[browser_id] = instance
        return instance

    async def open(self, *, headless: bool = False) -> dict[str, Any]:
        """Open a new Chrome browser window.

        Returns:
            Dict with browser_id, the initial tab_id, and a status.
        """
        try:
            instance = await self._create_instance(headless=headless)
        except BrowserOperationError:
            raise
        except Exception as e:
            raise BrowserOperationError(f"Failed to open browser: {e!s}") from e
        initial_tab_id = next(iter(instance._tabs))  # noqa: SLF001
        return {
            "browser_id": instance.browser_id,
            "tab_id": initial_tab_id,
            "status": "launched",
        }

    async def close_instance(self, browser_id: int) -> dict[str, Any]:
        """Close a browser instance by ID.

        Returns:
            Dict with browser_id and status.
        """
        inst = self._require_instance(browser_id)
        await inst._close()  # noqa: SLF001
        del self._instances[browser_id]
        return {"browser_id": browser_id, "status": "closed"}

    async def list_tabs(self, browser_id: int | None = None) -> list[dict]:
        """List tabs grouped by browser.

        Args:
            browser_id: If given, list only that browser's tabs.

        Raises:
            BrowserOperationError: If browser_id is given and not found.
        """
        if browser_id is not None:
            inst = self._require_instance(browser_id)
            return [
                {
                    "browser_id": inst.browser_id,
                    "tabs": await inst.list_tabs(),
                }
            ]
        result = []
        for bid, inst in sorted(self._instances.items()):
            result.append(
                {
                    "browser_id": bid,
                    "tabs": await inst.list_tabs(),
                }
            )
        return result

    async def open_tab(self, browser_id: int, url: str | None = None) -> dict[str, Any]:
        """Open a new tab in a specific browser.

        Returns:
            Dict with browser_id, the new tab_id, and status.
        """
        inst = self._require_instance(browser_id)
        tab_id = await inst.open_tab(url)
        return {"browser_id": browser_id, "tab_id": tab_id, "status": "opened"}

    async def close_tab(self, browser_id: int, tab_id: int) -> dict[str, Any]:
        """Close a tab in a specific browser.

        Returns:
            Dict with browser_id, tab_id, and status.
        """
        inst = self._require_instance(browser_id)
        await inst.close_tab(tab_id)
        return {"browser_id": browser_id, "tab_id": tab_id, "status": "closed"}

    async def navigate(self, browser_id: int, tab_id: int, url: str) -> dict[str, Any]:
        """Navigate an existing tab to ``url``.

        Returns:
            Dict with status.
        """
        inst = self._require_instance(browser_id)
        await inst.navigate(tab_id, url)
        return {"status": f"Navigated to: {url}"}

    async def eval_js(self, browser_id: int, tab_id: int, js_code: str) -> Any:
        """Execute JavaScript in the target tab."""
        inst = self._require_instance(browser_id)
        return await inst.eval_js(tab_id, js_code)

    async def wait_for(
        self, browser_id: int, tab_id: int, expression: str, *, timeout_ms: float
    ) -> Any:
        """Poll a JS expression until truthy or timeout in the target tab."""
        inst = self._require_instance(browser_id)
        return await inst.wait_for(tab_id, expression, timeout_ms=timeout_ms)

    async def screenshot(
        self, browser_id: int, tab_id: int, output_path: str | None = None
    ) -> str:
        """Capture a screenshot of the target tab's viewport."""
        inst = self._require_instance(browser_id)
        return await inst.screenshot(tab_id, output_path)

    async def page_snapshot(self, browser_id: int, tab_id: int) -> str:
        """Return the page's accessibility tree as agent-readable text."""
        inst = self._require_instance(browser_id)
        return await inst.page_snapshot(tab_id)

    async def page_click(
        self, browser_id: int, tab_id: int, ref: str, *, timeout_ms: float
    ) -> dict[str, Any]:
        """Click the element identified by ``ref`` in the target tab."""
        inst = self._require_instance(browser_id)
        return await inst.page_click(tab_id, ref, timeout_ms=timeout_ms)

    async def page_fill(
        self,
        browser_id: int,
        tab_id: int,
        ref: str,
        value: str,
        *,
        timeout_ms: float,
    ) -> dict[str, Any]:
        """Fill the element identified by ``ref`` with ``value``."""
        inst = self._require_instance(browser_id)
        return await inst.page_fill(tab_id, ref, value, timeout_ms=timeout_ms)

    async def page_hover(
        self, browser_id: int, tab_id: int, ref: str, *, timeout_ms: float
    ) -> dict[str, Any]:
        """Hover the element identified by ``ref`` in the target tab."""
        inst = self._require_instance(browser_id)
        return await inst.page_hover(tab_id, ref, timeout_ms=timeout_ms)

    async def page_upload(
        self,
        browser_id: int,
        tab_id: int,
        ref: str,
        files: list[str],
        *,
        timeout_ms: float,
    ) -> dict[str, Any]:
        """Set files on a file input identified by ``ref``."""
        inst = self._require_instance(browser_id)
        return await inst.page_upload(tab_id, ref, files, timeout_ms=timeout_ms)

    async def list_event_listeners(self, browser_id: int, tab_id: int) -> list[dict]:
        """List JS event listeners in the target tab."""
        inst = self._require_instance(browser_id)
        return await inst.list_event_listeners(tab_id)

    async def coverage_start(self, browser_id: int, tab_id: int) -> dict[str, Any]:
        """Enable precise block-level coverage on the target tab."""
        inst = self._require_instance(browser_id)
        return await inst.coverage_start(tab_id)

    async def coverage_snapshot(self, browser_id: int, tab_id: int) -> dict[str, Any]:
        """Read per-block hit counts on the target tab without stopping."""
        inst = self._require_instance(browser_id)
        return await inst.coverage_snapshot(tab_id)

    async def coverage_stop(self, browser_id: int, tab_id: int) -> dict[str, Any]:
        """Take a final coverage snapshot and stop recording on the target tab."""
        inst = self._require_instance(browser_id)
        return await inst.coverage_stop(tab_id)

    async def install_userscript(
        self, browser_id: int, name: str, source: str
    ) -> dict[str, Any]:
        """Install a userscript into the given browser's scope and reload."""
        inst = self._require_instance(browser_id)
        result = userscript_mod.install(browser_id, name, source)
        result["extension_id"] = await inst._reload_userscript_extension()  # noqa: SLF001
        return result

    async def remove_userscript(self, browser_id: int, name: str) -> dict[str, Any]:
        """Remove a userscript from the given browser's scope and reload."""
        inst = self._require_instance(browser_id)
        result = userscript_mod.remove(browser_id, name)
        result["extension_id"] = await inst._reload_userscript_extension()  # noqa: SLF001
        return result

    def list_userscripts(self, browser_id: int) -> list[dict[str, Any]]:
        """List installed userscripts for one browser from disk.

        Returns ``[]`` for a browser that was never opened (its
        per-browser dir doesn't exist). Unlike install/remove, this is
        a read and does not validate the browser is currently open.
        """
        return userscript_mod.list_scripts(browser_id)

    # --- wrap ----------------------------------------------------------

    async def _wrap_add(
        self,
        browser_id: int,
        tab_id: int,
        name: str,
        expr: str,
        install_fn,
    ) -> dict[str, Any]:
        """Install a wrap (call or access) into the browser's scope and reload.

        Validates that the target tab exists (so a stale ``--tab-id``
        errors cleanly), installs the wrap as a named userscript via
        ``install_fn`` into the given browser's scope, and reloads that
        browser's extension. The wrap takes effect on the next
        navigation (the userscript re-runs at ``document_start``).

        Returns:
            The install result from the wrap module.
        """
        inst = self._require_instance(browser_id)
        inst._require_tab(tab_id)  # noqa: SLF001
        result = install_fn(browser_id, name, expr)
        result["extension_id"] = await inst._reload_userscript_extension()  # noqa: SLF001
        return result

    async def wrap_calls_add(
        self, browser_id: int, tab_id: int, name: str, expr: str
    ) -> dict[str, Any]:
        """Install a call wrap on a named function and reload the extension."""
        return await self._wrap_add(
            browser_id, tab_id, name, expr, wrap_mod.install_call
        )

    async def wrap_access_add(
        self, browser_id: int, tab_id: int, name: str, expr: str
    ) -> dict[str, Any]:
        """Install an access wrap on a property accessor and reload the extension."""
        return await self._wrap_add(
            browser_id, tab_id, name, expr, wrap_mod.install_access
        )

    async def wrap_list(self, browser_id: int, tab_id: int) -> list[dict[str, Any]]:
        """List installed wraps for the given browser.

        Wraps are stored on disk as named userscripts scoped to the
        given browser (per ADR-0010). The ``--tab-id`` is validated
        for targeting consistency with the other wrap commands but
        does not filter the list.

        Returns:
            A list of ``{name, type, expr}`` dicts.
        """
        inst = self._require_instance(browser_id)
        inst._require_tab(tab_id)  # noqa: SLF001
        return wrap_mod.list_wraps(browser_id)

    async def wrap_remove(
        self, browser_id: int, tab_id: int, name: str
    ) -> dict[str, Any]:
        """Remove a wrap from the given browser's scope and reload its extension.

        The wrap stops recording on future navigations. Existing
        records in already-loaded tabs are not affected (the wrapper
        function is still in place until the page navigates).

        Returns:
            The remove result from the wrap module.
        """
        inst = self._require_instance(browser_id)
        inst._require_tab(tab_id)  # noqa: SLF001
        try:
            result = wrap_mod.remove(browser_id, name)
        except ValueError as exc:
            raise BrowserOperationError(str(exc)) from exc
        result["extension_id"] = await inst._reload_userscript_extension()  # noqa: SLF001
        return result

    async def wrap_dump(
        self, browser_id: int, tab_id: int, name: str | None = None
    ) -> list[dict[str, Any]]:
        """Read the per-tab wrap record array, optionally filtered to one wrap."""
        inst = self._require_instance(browser_id)
        return await inst.wrap_dump(tab_id, name)

    async def wrap_clear(self, browser_id: int, tab_id: int) -> dict[str, Any]:
        """Zero the per-tab wrap record array without navigating."""
        inst = self._require_instance(browser_id)
        return await inst.wrap_clear(tab_id)

    # --- logpoint ------------------------------------------------------

    async def logpoint_add(
        self,
        browser_id: int,
        tab_id: int,
        url: str,
        line: int,
        col: int,
        expr: str,
    ) -> dict[str, Any]:
        """Plant a non-pausing logpoint on the target tab."""
        inst = self._require_instance(browser_id)
        return await inst.logpoint_add(tab_id, url, line, col, expr)

    async def logpoint_list(self, browser_id: int, tab_id: int) -> list[dict[str, Any]]:
        """List planted logpoints on the target tab."""
        inst = self._require_instance(browser_id)
        return await inst.logpoint_list(tab_id)

    async def logpoint_dump(self, browser_id: int, tab_id: int) -> list[dict[str, Any]]:
        """Read the per-tab logpoint record array."""
        inst = self._require_instance(browser_id)
        return await inst.logpoint_dump(tab_id)

    async def logpoint_clear(self, browser_id: int, tab_id: int) -> dict[str, Any]:
        """Zero the per-tab logpoint record array without navigating."""
        inst = self._require_instance(browser_id)
        return await inst.logpoint_clear(tab_id)

    async def logpoint_remove(
        self, browser_id: int, tab_id: int, lp_id: str
    ) -> dict[str, Any]:
        """Remove a logpoint's CDP breakpoint and registry entry."""
        inst = self._require_instance(browser_id)
        return await inst.logpoint_remove(tab_id, lp_id)
