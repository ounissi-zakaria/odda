"""Browser automation module using patchright (Playwright)."""

from __future__ import annotations

import asyncio
import logging
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from patchright.async_api import (
    BrowserContext,
    CDPSession,
    Dialog,
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
from odda.chrome_args import build_chrome_args

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# Context lines around each page_find match (upstream parity: grep -C 3).
_FIND_CONTEXT_LINES = 3


BASE_PROFILE_DIR = Path.home() / ".config" / "odda" / "chrome-profile"

# Chrome-managed junk deleted from the base profile once by
# ``_slim_base_profile`` (called from ``init_chrome_profile`` after the user
# closes Chrome). Root junk is dropped at the profile root; profile junk one
# level inside ``Default`` / ``Profile N`` only, so deeper same-named dirs
# (extension/site storage) survive. Measured: 188MB -> 13MB, cookies intact.
_SEED_JUNK_ROOT: frozenset[str] = frozenset(
    {
        "component_crx_cache",
        "optimization_guide_model_store",
        "optimization_guide_prediction_model_downloads",
        "WasmTtsEngine",
        "Safe Browsing",
        "OnDeviceHeadSuggestModel",
        "ZxcvbnData",
        "GPUPersistentCache",
        "GraphiteDawnCache",
        "GrShaderCache",
        "ShaderCache",
        "CertificateRevocation",
        "Crashpad",
        "BrowserMetrics",
        "MEIPreload",
        "OptimizationHints",
        "extensions_crx_cache",
    }
)
# Cache dirs that live inside a profile dir (``Default`` / ``Profile N``).
_SEED_JUNK_PROFILE: frozenset[str] = frozenset(
    {
        "Cache",
        "Code Cache",
        "Media Cache",
        "GPUCache",
        "DawnWebGPUCache",
        "DawnGraphiteCache",
        "GraphiteDawnCache",
        "ShaderCache",
    }
)


def _slim_base_profile() -> None:
    """Delete Chrome-managed junk from ``BASE_PROFILE_DIR`` once.

    Called from :func:`init_chrome_profile` after the user closes Chrome, so
    the base profile stays slim for every subsequent seed copy. Deletes
    ``_SEED_JUNK_ROOT`` at the profile root and ``_SEED_JUNK_PROFILE`` one
    level inside each profile dir (``Default`` / ``Profile N``) only; deeper
    same-named dirs (extension/site storage) survive.
    """
    with suppress(OSError):
        for name in _SEED_JUNK_ROOT:
            shutil.rmtree(BASE_PROFILE_DIR / name, ignore_errors=True)
        for profile in BASE_PROFILE_DIR.iterdir():
            if profile.is_dir() and (
                profile.name == "Default" or profile.name.startswith("Profile ")
            ):
                for name in _SEED_JUNK_PROFILE:
                    shutil.rmtree(profile / name, ignore_errors=True)


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


def _headless_launch_user_agent(chrome_executable: str) -> str | None:
    """Build a headed-Chrome UA from the installed binary, or ``None``.

    Chrome brands ``--headless`` sessions ``HeadlessChrome/<version>`` and
    some origins reset such connections outright, so headless launches hand
    patchright a headed-shaped UA (same major version, Chrome's reduced
    form, headed token). The
    driver applies it via ``Emulation.setUserAgentOverride``, covering the
    wire header and page-visible ``navigator.userAgent`` for every page in
    the context. The version is probed from the actual binary; any failure
    returns ``None`` and the launch proceeds without an override.
    """
    try:
        proc = subprocess.run(  # noqa: S603
            [chrome_executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"\d+(?:\.\d+){3}", proc.stdout)
    if match is None:
        return None
    # Chrome's UA reduction: the UA string carries major.0.0.0; the full
    # build surfaces only in high-entropy client hints.
    version = f"{match.group(0).split('.')[0]}.0.0.0"
    if sys.platform.startswith("win"):
        ua_platform = "Windows NT 10.0; Win64; x64"
    elif sys.platform == "darwin":
        ua_platform = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        machine = platform.machine() or "x86_64"
        ua_platform = f"X11; Linux {machine}"
    return (
        f"Mozilla/5.0 ({ua_platform}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{version} Safari/537.36"
    )


def _prepare_user_data_dir() -> str:
    """Create a temp user data dir, optionally seeded from the base profile.

    The base profile is slimmed once by :func:`_slim_base_profile` during
    :func:`init_chrome_profile`, so the copy here is a plain ``copytree`` (no
    per-launch filtering).

    Returns:
        Path to a temporary directory that can be used as Chrome's
        user data directory.
    """
    temp_dir = tempfile.mkdtemp(prefix="odda_")
    if BASE_PROFILE_DIR.is_dir():
        with suppress(OSError):
            shutil.copytree(BASE_PROFILE_DIR, temp_dir, dirs_exist_ok=True)
    return temp_dir


def init_chrome_profile() -> dict[str, Any]:
    """Launch Chrome against the base profile dir so the user can configure it.

    Finds a Chrome executable, refuses to start if the base profile is
    already locked by a running Chrome, launches Chrome with
    ``--user-data-dir=<BASE_PROFILE_DIR>`` plus the minimal first-run
    flags, and blocks until the user closes the window. After close, slims
    Chrome-managed junk from the profile once (so each subsequent seed copy
    stays small). The configured profile is then copied by
    :func:`_prepare_user_data_dir` into each isolated browser session.

    Returns:
        ``{"chrome": <path>, "profile_dir": <path>, "status": "closed"}``.

    Raises:
        RuntimeError: If Chrome is not found, or if the base profile
            directory is locked by another Chrome instance.
    """
    chrome = _find_chrome_executable()
    profile_dir = BASE_PROFILE_DIR

    if (profile_dir / "SingletonLock").exists():
        msg = (
            f"Profile directory {profile_dir} appears to be locked "
            "by another Chrome instance. Close it first."
        )
        raise RuntimeError(msg)

    profile_dir.mkdir(parents=True, exist_ok=True)

    args = [
        chrome,
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    subprocess.run(args, check=True)  # noqa: S603
    _slim_base_profile()

    return {"chrome": chrome, "profile_dir": str(profile_dir), "status": "closed"}


class BrowserOperationError(Exception):
    """Raised when a browser/tab operation fails for a targetable reason.

    Carries a short message that surfaces to the agent as a tool
    error. Used for unknown browser/tab ids, mismatched
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


def _is_dialog_already_closed_error(exc: BaseException) -> bool:
    """Return True if exc means the dialog was closed/handled elsewhere.

    Two surfaces: Chrome's protocol error when no dialog is showing
    (``Page.handleJavaScriptDialog: No dialog is showing``), and the
    driver's own assertion when its Dialog object was already handled
    (``Cannot accept dialog which is already handled!``). Both mean a
    user close or another handler beat us to the dialog.
    """
    msg = str(exc).lower()
    return (
        "no dialog is showing" in msg
        or "already handled" in msg
        or "cannot accept dialog" in msg
    )


def _discard_task_result(task: asyncio.Task[Any] | None) -> None:
    """Discard a parked task's eventual result/exception.

    A task whose result nobody will read (a trigger action parked
    after losing the race to a dialog per ADR-0022, or a superseded
    liveness probe) would warn "exception was never retrieved" at GC
    time. When the task is already done (async context), retrieve
    the result now; otherwise schedule the retrieval as a done
    callback. ``None`` (no probe could be parked) is a no-op.
    """
    if task is None:
        return
    if task.done():
        with suppress(Exception):
            task.result()
    else:
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


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
        # Per-tab open-dialog registry (ADR-0022): ``{tab_id: {type,
        # message, default_value, dialog, probe_task}}``. Keyed by tab
        # at ``page.on("dialog")`` time — never trust the driver
        # Dialog object for openness (a user close in the real browser
        # leaves it stale). The liveness probe (a parked
        # ``page.evaluate("1")``) completes exactly when the dialog is
        # no longer open; the MCP ``dialog_handle`` tool (issue 02)
        # calls ``dialog.accept()/dismiss()`` on the stored object.
        self._dialogs: dict[int, dict[str, Any]] = {}
        # Per-tab trigger-race waiters: Futures resolved by the dialog
        # listener when a dialog opens mid-action. A trigger tool
        # races its action task against its own future
        # (FIRST_COMPLETED); dialog wins → the tool returns dialog
        # details, action wins → normal result. A list, not one slot:
        # multiple trigger tools can race concurrently on one tab
        # (e.g. an eval and a click) and each must be woken.
        self._dialog_waiters: dict[int, list[asyncio.Future[None]]] = {}

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
        page.on("dialog", lambda dialog: self._on_dialog_open(tab_id, dialog))
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
        cdp = self._cdp_sessions.pop(tab_id, None)
        if cdp is not None:
            # detach is async and this handler is sync: schedule it,
            # discarding the result (a dying target rejects the send
            # — the session is dead either way).
            task = asyncio.get_running_loop().create_task(cdp.detach())
            task.add_done_callback(_discard_task_result)
        # A closed tab can no longer hold a dialog; drop its registry
        # entry. The probe task's own done-callback clears the entry
        # too (it may still be parked when the tab is torn down from
        # elsewhere).
        if tab_id in self._dialogs:
            self._clear_dialog_entry(tab_id)
        # Discard pending trigger-race waiters without resolving them:
        # a closed tab never opens a dialog, so the races must resolve
        # via their action tasks instead (they fail with target-closed
        # errors, which each action's error handling converts).
        self._dialog_waiters.pop(tab_id, None)
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

    def _require_ready_tab(self, tab_id: int) -> Page:
        """Return the tab's Page, raising unless the tab is dialog-free.

        The single prologue for every tool that dispatches into a
        tab: unknown tab → the not-found error; open dialog on the
        tab → the reject error (ADR-0022 block gate: the renderer
        is frozen behind a native dialog; dispatching would hang the
        tool). Tools that must run with a dialog open (dialog_handle,
        close_tab) use ``_require_tab`` directly.
        """
        page = self._require_tab(tab_id)
        self._reject_if_dialog(tab_id)
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

    async def _run_action(
        self,
        tab_id: int,
        verb: str,
        action: Callable[[], Awaitable[Any]],
        convert: Callable[[Exception], BrowserOperationError | None] | None = None,
    ) -> Any:
        """Run a tab action with the shared error conversion.

        Target-closed errors tear down the tab's state and raise the
        "<verb> closed" error (the tab is gone). Everything else goes
        to ``convert`` — each tool supplies its own mapping (e.g.
        timeout → the SPA hint for navigate, the stale-ref hint for
        ref actions); returning ``None`` from ``convert`` re-raises
        the original exception. ``convert=None`` re-raises anything
        that is not target-closed.
        """
        try:
            return await action()
        except Exception as exc:
            if _is_target_closed_error(exc):
                self._on_page_close(tab_id)
                raise BrowserOperationError(
                    f"Tab {tab_id} closed during {verb}."
                ) from exc
            if convert is not None:
                converted = convert(exc)
                if converted is not None:
                    raise converted from exc
            raise

    # --- dialogs (ADR-0022) --------------------------------------------

    def _on_dialog_open(self, tab_id: int, dialog: Dialog) -> None:
        """Record a newly opened native dialog; never handle it.

        Suppresses patchright's no-listener auto-dismiss (registered
        in ``_register_page`` for every tab). Records the registry
        entry (keyed by tab, never trusting the driver Dialog object
        for openness), parks the liveness probe (a plain
        ``page.evaluate("1")`` — it has no dialog gate and completes
        exactly when the dialog closes), and wakes any trigger tool
        racing its action against this dialog.
        """
        previous = self._dialogs.get(tab_id)
        if previous is not None:
            # A chained dialog replaced a still-registered one (the
            # previous dialog was handled but its probe hasn't
            # completed yet): discard the previous probe — a done
            # callback from it must not clear the new entry (the
            # identity guard in _on_probe_done handles it, and a
            # dangling probe result is harmless to leave unretrieved:
            # evaluate("1") cannot fail on a live tab).
            _discard_task_result(previous.get("probe_task"))
        entry = {
            "type": dialog.type,
            "message": dialog.message,
            "default_value": dialog.default_value,
        }
        page = self._tabs.get(tab_id)
        probe_task = None
        if page is not None and not page.is_closed():
            probe_task = asyncio.create_task(page.evaluate("1"))
            probe_task.add_done_callback(
                lambda _task: self._on_probe_done(tab_id, probe_task)
            )
        self._dialogs[tab_id] = {
            **entry,
            "dialog": dialog,
            "probe_task": probe_task,
        }
        # Wake every trigger tool racing on this tab: resolve their
        # waiter futures and clear the slot list — a second dialog on
        # the same tab (chained) starts fresh waiters.
        waiters = self._dialog_waiters.pop(tab_id, None)
        if waiters:
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(None)

    def _on_probe_done(self, tab_id: int, probe_task: asyncio.Task[Any]) -> None:
        """Probe done-callback: clear the entry, purge stale state.

        The probe (``page.evaluate("1")``) completing means the dialog
        is no longer open — handled via ``dialog_handle`` (issue 02)
        or closed by the user in the real browser. A done-callback
        cannot await; the purge is its own task because the driver's
        ``dialog.accept()`` is async and the failed CDP call still
        purges its internal state (purge-before-send, verified on the
        pin).
        """
        # Retrieve the result/exception so a failed probe (tab torn
        # down mid-dialog) doesn't warn "exception never retrieved" at
        # GC time; the value itself is uninteresting.
        with suppress(Exception):
            probe_task.result()
        entry = self._dialogs.get(tab_id)
        # Guard: a second dialog may have replaced this entry (chained
        # dialogs on the same tab). Only clear if this probe owns it.
        if entry is None or entry.get("probe_task") is not probe_task:
            return
        self._clear_dialog_entry(tab_id)
        asyncio.get_running_loop().create_task(self._purge_dialog_state(entry))

    def _clear_dialog_entry(self, tab_id: int) -> None:
        """Drop a tab's dialog registry entry."""
        self._dialogs.pop(tab_id, None)

    async def _purge_dialog_state(self, entry: dict[str, Any]) -> None:
        """Best-effort purge the driver's stale dialog state.

        The dialog the probe watched was closed by something other
        than ``dialog_handle`` (the user closed it in the real
        browser, or raw CDP). The driver's Dialog object is stale —
        calling ``accept()`` on it fails the CDP call ("no dialog is
        showing") but still purges the driver's ``_openedDialogs``
        entry before the send, so the driver stops gating non-stalling
        evaluations (title/snapshot helpers) on this dialog.
        """
        dialog = entry.get("dialog")
        if dialog is not None:
            with suppress(Exception):
                await dialog.accept()

    def _reject_if_dialog(self, tab_id: int) -> None:
        """Raise if the tab has an open dialog.

        The clean pre-check: if the registry has an entry, the renderer
        is frozen — dispatching into it would hang the tool inside the
        frozen dialog state. Reject the call instead: the agent
        handles the dialog (``dialog_handle`` or human close) and
        retries. Blocking set only — the trigger tools race their own
        action against a dialog opening mid-action.
        """
        entry = self._dialogs.get(tab_id)
        if entry is not None:
            raise BrowserOperationError(
                f"Tab {tab_id} has an open {entry['type']} dialog "
                f"({entry['message']!r}) — handle it with dialog_handle, "
                "then retry this call."
            )

    async def _race_dialog(
        self, tab_id: int, action: Callable[[], Awaitable[Any]]
    ) -> tuple[bool, Any]:
        """Race an action against a dialog opening mid-action.

        Trigger-tool wrapper (ADR-0022 trigger rule): the action
        starts with no dialog open on the tab (the block gate ran
        first). If the action itself opens a dialog — a click that
        fires ``confirm()``, an eval running ``alert()`` — the dialog
        listener resolves this per-tab waiter future and the race
        resolves ``(True, {"dialog": ...})`` immediately, leaving the
        action task parked in the background (it completes when the
        dialog is handled; its result is discarded). If the action
        completes first, the race resolves ``(False, result)``.
        """
        waiter = asyncio.get_running_loop().create_future()
        self._dialog_waiters.setdefault(tab_id, []).append(waiter)
        action_task = asyncio.create_task(action())
        action_task.add_done_callback(_discard_task_result)
        try:
            done, _pending = await asyncio.wait(
                {action_task, waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if waiter in done:
                return True, await self._open_dialog_result(tab_id)
            return False, await asyncio.shield(action_task)
        finally:
            waiters = self._dialog_waiters.get(tab_id)
            if waiters is not None:
                with suppress(ValueError):
                    waiters.remove(waiter)
                if not waiters:
                    self._dialog_waiters.pop(tab_id, None)

    @staticmethod
    def _dialog_details(entry: dict[str, Any], tab_id: int) -> dict[str, Any]:
        """Shape a registry entry as the public dialog record.

        Returns ``{type, message, default_value, tab_id}``.
        """
        return {
            "type": entry["type"],
            "message": entry["message"],
            "default_value": entry["default_value"],
            "tab_id": tab_id,
        }

    async def _open_dialog_result(self, tab_id: int) -> dict[str, Any]:
        """Return the ``{"dialog": ...}`` result for a won trigger race."""
        entry = self._dialogs.get(tab_id)
        if entry is None:
            raise BrowserOperationError(
                f"Dialog race resolved for tab {tab_id} but no dialog "
                "is registered — this is a bug in odda's dialog engine."
            )
        return {"dialog": self._dialog_details(entry, tab_id)}

    def open_dialogs(self) -> list[dict[str, Any]]:
        """Return this browser's open dialogs, oldest tab first.

        One ``{tab_id, type, message, default_value}`` per tab with
        an open dialog. Read-only observation; ``close_tab`` reads
        it for the tab it is about to close.
        """
        return [
            self._dialog_details(entry, tab_id)
            for tab_id, entry in self._dialogs.items()
        ]

    async def handle_dialog(
        self, tab_id: int, action: str, prompt_text: str | None = None
    ) -> dict[str, Any]:
        """Accept or dismiss the tab's open dialog.

        The single sanctioned way to resolve a dialog (ADR-0022):
        everything else leaves it open. ``prompt_text`` supplies the
        answer for ``prompt`` dialogs (accept-only; ignored
        otherwise). On success the registry entry is cleared at once
        — tools the dialog rejected can be retried immediately.

        Args:
            tab_id: Target tab.
            action: ``"accept"`` or ``"dismiss"``.
            prompt_text: Optional text to answer a ``prompt`` dialog
                with (accept-only; ignored otherwise).

        Raises:
            BrowserOperationError: If the tab has no open dialog.
            BrowserOperationError: If the driver reports the dialog
                was already closed (the user beat the call to it).

        Returns:
            ``{"handled": True, "action": ..., "tab_id": ...}``.
        """
        if action not in ("accept", "dismiss"):
            raise BrowserOperationError(
                f"Invalid dialog action {action!r}; use 'accept' or 'dismiss'."
            )
        entry = self._dialogs.get(tab_id)
        if entry is None:
            raise BrowserOperationError(
                f"Tab {tab_id} in browser {self.browser_id} has no open dialog."
            )
        dialog = entry["dialog"]
        try:
            if action == "accept":
                # Human parity: a user clicking OK on a prefilled prompt
                # submits the prefill. The driver drops a None promptText
                # and CDP then answers empty, so supply the dialog's
                # default_value when the caller omits prompt_text.
                text = prompt_text
                if text is None and entry["type"] == "prompt":
                    text = entry["default_value"]
                await dialog.accept(text)
            else:
                await dialog.dismiss()
        except Exception as exc:
            if _is_target_closed_error(exc):
                raise BrowserOperationError(
                    f"Tab {tab_id} closed while handling its dialog."
                ) from exc
            if _is_dialog_already_closed_error(exc):
                # The dialog was closed by someone else (the user in
                # the real browser, raw CDP). The registry entry is
                # cleared by the probe completing; report neutrally.
                raise BrowserOperationError(
                    f"Dialog on tab {tab_id} was already closed "
                    "(handled by the user or closed in the browser)."
                ) from exc
            raise
        # The driver answered: the dialog is gone now. Clear the
        # entry immediately — under drop semantics the reject gate is
        # one-shot, so a retried call must not lose a race against
        # the probe's own pipe round-trip. The probe's done-callback
        # hits the identity guard and stops there (no purge needed:
        # dialog_handle's own accept/dismiss already cleaned the
        # driver's state); its result is retrieved before the guard.
        if self._dialogs.get(tab_id) is entry:
            self._clear_dialog_entry(tab_id)
        return {"handled": True, "action": action, "tab_id": tab_id}

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

    async def open_tab(self, url: str | None = None) -> dict[str, Any] | int:
        """Open a new tab, optionally navigating to ``url``.

        context.on('page') registers the new page synchronously during
        new_page(); we look up the assigned tab_id by identity.

        Trigger tool (ADR-0022) when ``url`` is given: the goto is
        raced against the new tab's dialog waiters, so a page that
        opens a dialog at load returns ``{dialog: {...}}`` promptly
        instead of hanging the goto behind the frozen renderer — the
        same trigger rule as ``navigate``. Without ``url`` there is no
        page load to race; the blank tab id is returned.

        Returns:
            The new tab_id, or ``{dialog: ...}`` when a dialog opened.
        """
        page = await self.context.new_page()
        tab_id = self._tab_id_for_page(page)
        if url is None:
            return tab_id
        won, result = await self._race_dialog(tab_id, lambda: page.goto(url))
        return result if won else tab_id

    async def close_tab(self, tab_id: int) -> None:
        """Close a tab by id. No-op if already closed."""
        page = self._require_tab(tab_id)
        with suppress(Exception):
            await page.close()
        self._on_page_close(tab_id)

    async def navigate(
        self,
        tab_id: int,
        url: str,
        *,
        timeout_ms: float = 30000.0,
        wait_until: str = "load",
    ) -> dict[str, Any]:
        """Navigate an existing tab to ``url``.

        Trigger tool (ADR-0022): if the navigation opens a dialog
        (e.g. a ``beforeunload`` handler), the result carries the
        dialog's details instead of the status string; the goto
        parks in the background and completes once the dialog is
        handled.

        Args:
            tab_id: Target tab.
            url: URL to navigate to.
            timeout_ms: ``page.goto`` timeout in milliseconds (default
                30000, matching Playwright).
            wait_until: Playwright lifecycle event to wait for — one of
                ``commit``, ``domcontentloaded``, ``load``,
                ``networkidle`` (default ``load``).

        Returns:
            ``{"status": "Navigated to: <url>"}``, or the open
            dialog's details when the navigation opened one.
        """
        page = self._require_ready_tab(tab_id)

        def _convert(exc: Exception) -> BrowserOperationError | None:
            msg = str(exc)
            if "timeout" in msg.lower() or "Timeout" in type(exc).__name__:
                hint = (
                    " For SPAs that don't fire `load`, use the `wait_for`"
                    " tool with a JS expression after navigate to poll"
                    " for a condition."
                )
                return BrowserOperationError(
                    f"Failed to navigate: {msg} (wait-until `{wait_until}`){hint}"
                )
            return BrowserOperationError(f"Failed to navigate: {msg}")

        won, result = await self._race_dialog(
            tab_id,
            lambda: self._run_action(
                tab_id,
                "navigation",
                lambda: page.goto(url, timeout=timeout_ms, wait_until=wait_until),
                _convert,
            ),
        )
        return result if won else {"status": f"Navigated to: {url}"}

    async def eval_js(self, tab_id: int, js_code: str) -> Any:
        """Execute JavaScript in the target tab's main world.

        Trigger tool (ADR-0022): if the evaluated code opens a native
        dialog (``alert``/``confirm``/``prompt``), this returns
        ``{dialog: {type, message, default_value, tab_id}}`` instead
        of the code's result — the code resumes when the dialog is
        handled. Non-target-closed errors are still converted to
        ``"JavaScript error: ..."`` strings on the action-wins path.
        """
        page = self._require_ready_tab(tab_id)
        _won, result = await self._race_dialog(
            tab_id,
            lambda: self._run_action(
                tab_id,
                "eval",
                lambda: page.evaluate(js_code, isolated_context=False),
                lambda exc: BrowserOperationError(f"JavaScript error: {exc!s}"),
            ),
        )
        return result

    async def wait_for(self, tab_id: int, expression: str, *, timeout_ms: float) -> Any:
        """Poll a JS expression until truthy or timeout in the target tab.

        A thrown error inside the expression is treated as falsy and
        polling continues — ``wait-for`` exists for "the DOM isn't ready
        yet", so a null deref (e.g. ``document.querySelector('#root').children``
        while ``#root`` is still absent) keeps polling until truthy or
        timeout rather than crashing on the first throw.
        """
        page = self._require_ready_tab(tab_id)
        fn = (
            "() => { try { const v = (" + expression + "); return v ? v : false; }"
            " catch (e) { return false; } }"
        )

        async def _wait() -> Any:
            handle = await self._run_action(
                tab_id,
                "wait-for",
                lambda: page.wait_for_function(fn, timeout=timeout_ms),
                lambda exc: (
                    BrowserOperationError(
                        f"Timeout after {timeout_ms}ms waiting for expression "
                        "to become truthy"
                    )
                    if "timeout" in str(exc).lower() or "Timeout" in type(exc).__name__
                    else None
                ),
            )
            try:
                return await handle.json_value()
            except Exception:
                return str(handle)

        _won, result = await self._race_dialog(tab_id, _wait)
        return result

    async def screenshot(self, tab_id: int, output_path: str | None = None) -> str:
        """Capture a JPEG screenshot of the target tab's viewport.

        Args:
            tab_id: Target tab.
            output_path: Optional path to write the JPEG to. When None,
                a temp file path under the system temp dir is generated
                (legacy behavior). When given, the directory must exist.
        """
        page = self._require_ready_tab(tab_id)
        if output_path is not None:
            target = Path(output_path)
            target.parent.mkdir(parents=True, exist_ok=True)
        else:
            target = Path(tempfile.gettempdir()) / f"screenshot_{int(time.time())}.jpeg"

        async def _shoot() -> str:
            await page.screenshot(path=str(target), type="jpeg", full_page=False)
            return str(target)

        return await self._run_action(
            tab_id,
            "screenshot",
            _shoot,
            lambda e: BrowserOperationError(f"Screenshot error: {e!s}"),
        )

    # --- page interaction --------------------------------------------

    async def page_snapshot(
        self,
        tab_id: int,
        *,
        depth: int | None = None,
        boxes: bool = False,
    ) -> str:
        """Return the page's accessibility tree as agent-readable text.

        Calls Playwright's ``aria_snapshot(mode="ai")`` which returns
        a YAML-ish serialization of the a11y tree with ``[ref=eN]`` tags
        (or ``[ref=f<frameSeq>eN]`` inside iframes). The agent greps the
        text for the element it wants and passes the ref to ``click``,
        ``fill``, ``hover``, or ``upload``. Every call walks the whole
        page — never a subtree — so the page-side ref map is always
        complete and refs from any snapshot stay valid until their
        element leaves the DOM. ``depth`` caps the rendered tree depth
        (boundary nodes render without children; the walk still visits
        and stamps everything below the cap); ``boxes`` adds
        ``[box=x,y,width,height]`` per line (source for coordinate
        clicks).
        """
        page = self._require_ready_tab(tab_id)
        return await self._run_action(
            tab_id,
            "snapshot",
            # boxes or None: False must serialize as absent, not disabled.
            lambda: page.aria_snapshot(mode="ai", depth=depth, boxes=boxes or None),
            lambda e: BrowserOperationError(f"Snapshot error: {e!s}"),
        )

    async def page_find(
        self, tab_id: int, pattern: re.Pattern[str], *, boxes: bool = False
    ) -> str:
        """Search a fresh snapshot; return matching regions, not the tree.

        Cheap way to locate an element and its ref on a large page: a
        full ``mode="ai"`` snapshot is still taken server-side, but only
        the matched lines (each with a few lines of context, overlapping
        windows coalesced, and the match's ancestor path from the tree
        root prepended) come back.
        """
        text = await self.page_snapshot(tab_id, boxes=boxes)
        lines = text.splitlines()
        matched = [i for i, line in enumerate(lines) if pattern.search(line)]
        if not matched:
            return f"No matches for /{pattern.pattern}/ in snapshot"

        windows: list[tuple[int, int]] = []
        for i in matched:
            lo = max(0, i - _FIND_CONTEXT_LINES)
            hi = min(len(lines) - 1, i + _FIND_CONTEXT_LINES)
            # +1: adjacent windows merge too (upstream parity).
            if windows and lo <= windows[-1][1] + 1:
                windows[-1] = (windows[-1][0], hi)
            else:
                windows.append((lo, hi))

        blocks = []
        for w, (lo, hi) in enumerate(windows, 1):
            first_match = next(i for i in matched if lo <= i <= hi)
            chain = self._ancestor_chain(lines, first_match)
            body = "\n".join(lines[lo : hi + 1])
            blocks.append(f"[{w}] {' > '.join(chain)}\n{body}")
        return "\n\n".join(blocks)

    @staticmethod
    def _ancestor_chain(lines: list[str], idx: int) -> list[str]:
        """Root-to-node ancestor crumbs for snapshot line ``idx``.

        Walks upward collecting strictly-dedenting non-blank lines —
        YAML indentation is the tree structure. Crumbs keep their
        ``[ref=eN]`` tags (ancestor refs are valid scoping targets),
        minus the list marker and trailing container colon.
        """
        chain: list[str] = []
        indent = len(lines[idx]) - len(lines[idx].lstrip(" "))
        for line in reversed(lines[:idx]):
            if not line.strip():
                continue
            line_indent = len(line) - len(line.lstrip(" "))
            if line_indent < indent:
                crumb = line.strip().removeprefix("- ")
                chain.append(crumb.rstrip(":"))
                indent = line_indent
        chain.reverse()
        return chain

    def _ref_convert(
        self, ref: str, verb: str, timeout_ms: float
    ) -> Callable[[Exception], BrowserOperationError | None]:
        """Build the ref-tool error converter.

        A timeout may mean the ref is stale (element removed) or the
        element is present but not actionable (disabled, covered by
        an overlay); the error message reflects both possibilities.
        Anything else becomes the generic ``<Verb> error`` message.
        """

        def convert(exc: Exception) -> BrowserOperationError | None:
            if "timeout" in str(exc).lower() or "Timeout" in type(exc).__name__:
                return BrowserOperationError(
                    f"ref {ref} did not resolve or become actionable "
                    f"within {timeout_ms}ms (the element may have been "
                    f"removed, or it may be disabled or covered by an "
                    f"overlay; take a new snapshot if stale)"
                )
            return BrowserOperationError(f"{verb.capitalize()} error: {exc!s}")

        return convert

    async def _ref_tool(
        self,
        tab_id: int,
        ref: str,
        verb: str,
        action: Callable[[], Awaitable[Any]],
        status: dict[str, Any],
        *,
        timeout_ms: float,
    ) -> dict[str, Any]:
        """Run a ref-driven tool: gate, race the action, shape the result.

        The whole body every ref tool shares (ADR-0022 trigger rule):
        resolve the locator outside the race (a stale ref fails fast
        in the race's action, not at locator build), convert errors
        via the ref converter, and return ``status`` unless a dialog
        opened mid-action.
        """
        self._require_ready_tab(tab_id)
        won, result = await self._race_dialog(
            tab_id,
            lambda: self._run_action(
                tab_id, verb, action, self._ref_convert(ref, verb, timeout_ms)
            ),
        )
        return result if won else status

    async def page_click(
        self, tab_id: int, ref: str, *, timeout_ms: float
    ) -> dict[str, Any]:
        """Click the element identified by ``ref`` (plain left-click).

        Uses Playwright's ``aria-ref`` selector engine to resolve the ref
        to a Locator, then clicks it. If the ref no longer resolves
        (element removed, navigated away), the Playwright timeout is
        converted to a clean ``BrowserOperationError``.
        """
        locator = self._require_ready_tab(tab_id).locator(f"aria-ref={ref}")
        return await self._ref_tool(
            tab_id,
            ref,
            "click",
            lambda: locator.click(timeout=timeout_ms),
            {"status": "clicked", "ref": ref},
            timeout_ms=timeout_ms,
        )

    async def page_click_coords(
        self, tab_id: int, *, x: float, y: float
    ) -> dict[str, Any]:
        """Click at viewport coordinates (raw trusted event dispatch).

        Uses Playwright's ``page.mouse.click(x, y)``: a CDP
        ``Input.dispatchMouseEvent`` move+down+up at viewport-relative
        CSS-pixel coordinates. No element resolution, no actionability
        checks, no retries — the event lands on whatever renders at that
        point (iframes included), and clicking empty space is a success
        no-op. This is the escape hatch for cases the ref path cannot
        express: elements behind overlays, canvas, custom hit-testing,
        deliberate off-center clicks, and coordinates taken from a
        screenshot. There is nothing to time out on, so no timeout
        applies.
        """
        page = self._require_ready_tab(tab_id)
        won, _ = await self._race_dialog(
            tab_id,
            lambda: self._run_action(
                tab_id,
                "click",
                lambda: page.mouse.click(x, y),
                lambda e: BrowserOperationError(f"Click error: {e!s}"),
            ),
        )
        if won:
            return await self._open_dialog_result(tab_id)
        return {"status": "clicked", "x": x, "y": y}

    async def page_hover_coords(
        self, tab_id: int, *, x: float, y: float
    ) -> dict[str, Any]:
        """Hover at viewport coordinates (raw trusted event dispatch).

        Uses Playwright's ``page.mouse.move(x, y)``: the same raw CDP
        event dispatch as a coordinate click, without the button press.
        Whatever renders at that point (iframes included) receives the
        mouseover/mousemove/mouseenter cascade. No element resolution,
        no actionability checks, no scrolling, no timeout.
        """
        page = self._require_ready_tab(tab_id)
        won, _ = await self._race_dialog(
            tab_id,
            lambda: self._run_action(
                tab_id,
                "hover",
                lambda: page.mouse.move(x, y),
                lambda e: BrowserOperationError(f"Hover error: {e!s}"),
            ),
        )
        if won:
            return await self._open_dialog_result(tab_id)
        return {"status": "hovered", "x": x, "y": y}

    async def page_fill(
        self, tab_id: int, ref: str, value: str, *, timeout_ms: float
    ) -> dict[str, Any]:
        """Fill the element identified by ``ref`` with ``value``.

        Clears the field first, then types the value (Playwright's
        ``locator.fill()``). Works on text inputs, textareas, contenteditable
        elements, checkboxes (``"true"``/``"false"``), radios, and selects.
        """
        locator = self._require_ready_tab(tab_id).locator(f"aria-ref={ref}")
        return await self._ref_tool(
            tab_id,
            ref,
            "fill",
            lambda: locator.fill(value, timeout=timeout_ms),
            {"status": "filled", "ref": ref},
            timeout_ms=timeout_ms,
        )

    async def page_hover(
        self, tab_id: int, ref: str, *, timeout_ms: float
    ) -> dict[str, Any]:
        """Hover the element identified by ``ref``.

        Auto-scrolls the element into view (Playwright's actionability
        check) before dispatching the hover.
        """
        locator = self._require_ready_tab(tab_id).locator(f"aria-ref={ref}")
        return await self._ref_tool(
            tab_id,
            ref,
            "hover",
            lambda: locator.hover(timeout=timeout_ms),
            {"status": "hovered", "ref": ref},
            timeout_ms=timeout_ms,
        )

    async def page_upload(
        self, tab_id: int, ref: str, files: list[str], *, timeout_ms: float
    ) -> dict[str, Any]:
        """Set files on a file input identified by ``ref``.

        Wraps Playwright's ``locator.set_input_files(files)``. Pass one
        path for a single file input, or multiple paths for
        ``<input type="file" multiple>``.
        """
        locator = self._require_ready_tab(tab_id).locator(f"aria-ref={ref}")
        return await self._ref_tool(
            tab_id,
            ref,
            "upload",
            lambda: locator.set_input_files(files, timeout=timeout_ms),
            {"status": "uploaded", "ref": ref, "files": files},
            timeout_ms=timeout_ms,
        )

    async def list_event_listeners(self, tab_id: int) -> list[dict]:
        """List JavaScript event listeners on window and document.

        Uses DOMDebugger.getEventListeners so we get script IDs, line
        numbers, and column numbers. Only Debugger.enable is required;
        Runtime, Console, and Page domains are intentionally left
        disabled to avoid detection leaks.
        """
        self._require_ready_tab(tab_id)
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
        self._require_ready_tab(tab_id)
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
        self._require_ready_tab(tab_id)
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
        self._require_ready_tab(tab_id)
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
        # Detaching a per-tab CDP session parks forever while a native
        # dialog is open on that tab (the driver can't complete the
        # detach). The context close below tears the sessions down
        # anyway, so the detaches are best-effort with a short budget
        # and skipped entirely for tabs with an open dialog.
        for tab_id in list(self._cdp_sessions.keys()):
            if tab_id in self._dialogs:
                continue
            with suppress(Exception):
                await asyncio.wait_for(self._cdp_sessions[tab_id].detach(), 5.0)
        self._cdp_sessions.clear()
        self._tabs.clear()
        # Drop every open-dialog registry entry — the tabs (and their
        # dialogs) are going away with the context.
        self._dialogs.clear()
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

    def list_instances(self) -> list[dict]:
        """List open browser instances with tab counts.

        Backs the ``browser_list`` tool and the session's
        shutdown sweep. Returns ``[{browser_id, tab_count}]`` with
        ``tab_count`` as ``len(_tabs)`` (raw — may include pages whose
        underlying Chrome has died if no close event fired).
        """
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

    async def _create_instance(self, *, headless: bool = True) -> BrowserInstance:
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

        # Headless UA normalization: probe once, off the event loop.
        user_agent = (
            await asyncio.to_thread(_headless_launch_user_agent, chrome_executable)
            if headless
            else None
        )
        playwright = await async_playwright().start()
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            executable_path=chrome_executable,
            headless=headless,
            # Headed UA, applied driver-side context-wide (see helper).
            user_agent=user_agent,
            proxy=proxy_config,
            ignore_https_errors=True,
            # ignore_default_args=True (bool) so Chrome receives only our
            # redeclared args (src/odda/chrome_args.py), which carry the m150
            # model-store suppression names in --disable-features. See
            # chrome_args.py and the AGENTS.md drift note for the two
            # deliberate omissions (password-store/mock-keychain).
            ignore_default_args=True,
            args=build_chrome_args(
                user_data_dir,
                self.proxy.proxy_url if self.proxy else None,
                headless=headless,
            ),
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

    async def open(self, *, headless: bool = True) -> dict[str, Any]:
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

        Trigger tool when ``url`` is given (ADR-0022): a page that
        opens a dialog at load resolves with the dialog's details
        instead of the open status; the goto completes in the
        background once the dialog is handled.

        Returns:
            Dict with browser_id, the new tab_id, and status — or
            with the open dialog's details.
        """
        inst = self._require_instance(browser_id)
        result = await inst.open_tab(url)
        if isinstance(result, dict):
            return {"browser_id": browser_id, **result}
        return {"browser_id": browser_id, "tab_id": result, "status": "opened"}

    async def page_snapshot(
        self,
        browser_id: int,
        tab_id: int,
        *,
        depth: int | None = None,
        boxes: bool = False,
    ) -> str:
        """Return the page's accessibility tree as agent-readable text."""
        inst = self._require_instance(browser_id)
        return await inst.page_snapshot(tab_id, depth=depth, boxes=boxes)

    async def page_find(
        self,
        browser_id: int,
        tab_id: int,
        pattern: re.Pattern[str],
        *,
        boxes: bool = False,
    ) -> str:
        """Return matching snapshot regions for a regex, not the tree."""
        inst = self._require_instance(browser_id)
        return await inst.page_find(tab_id, pattern, boxes=boxes)

    async def close_tab(self, browser_id: int, tab_id: int) -> dict[str, Any]:
        """Close a tab in a specific browser.

        If the closed tab had an open dialog (ADR-0022), the result
        carries ``closed_dialog: {type, message}`` so the agent knows
        what they destroyed with the close. The lookup happens before
        the close.

        Returns:
            Dict with browser_id, tab_id, and status; plus
            ``closed_dialog`` when a dialog was open.
        """
        inst = self._require_instance(browser_id)
        tab_dialog = next(
            (d for d in inst.open_dialogs() if d["tab_id"] == tab_id), None
        )
        closed_dialog = (
            {"type": tab_dialog["type"], "message": tab_dialog["message"]}
            if tab_dialog is not None
            else None
        )
        await inst.close_tab(tab_id)
        result = {"browser_id": browser_id, "tab_id": tab_id, "status": "closed"}
        if closed_dialog is not None:
            result["closed_dialog"] = closed_dialog
        return result

    async def navigate(
        self,
        browser_id: int,
        tab_id: int,
        url: str,
        *,
        timeout_ms: float = 30000.0,
        wait_until: str = "load",
    ) -> dict[str, Any]:
        """Navigate an existing tab to ``url``.

        Trigger tool (ADR-0022): if the navigation opens a dialog
        (e.g. a ``beforeunload`` handler), the result carries the
        dialog details instead of the status string.

        Returns:
            Dict with status, or with dialog details.
        """
        inst = self._require_instance(browser_id)
        return await inst.navigate(
            tab_id, url, timeout_ms=timeout_ms, wait_until=wait_until
        )

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

    async def page_click(
        self, browser_id: int, tab_id: int, ref: str, *, timeout_ms: float
    ) -> dict[str, Any]:
        """Click the element identified by ``ref`` in the target tab."""
        inst = self._require_instance(browser_id)
        return await inst.page_click(tab_id, ref, timeout_ms=timeout_ms)

    async def page_click_coords(
        self, browser_id: int, tab_id: int, *, x: float, y: float
    ) -> dict[str, Any]:
        """Click at viewport coordinates in the target tab."""
        inst = self._require_instance(browser_id)
        return await inst.page_click_coords(tab_id, x=x, y=y)

    async def page_hover_coords(
        self, browser_id: int, tab_id: int, *, x: float, y: float
    ) -> dict[str, Any]:
        """Hover at viewport coordinates in the target tab."""
        inst = self._require_instance(browser_id)
        return await inst.page_hover_coords(tab_id, x=x, y=y)

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

        Validates that the target tab exists (so a stale ``tab_id``
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
        given browser (per ADR-0010). The ``tab_id`` is validated
        for targeting consistency with the other wrap tools but
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

    async def handle_dialog(
        self,
        browser_id: int,
        tab_id: int,
        action: str,
        prompt_text: str | None = None,
    ) -> dict[str, Any]:
        """Accept or dismiss the tab's open dialog."""
        inst = self._require_instance(browser_id)
        return await inst.handle_dialog(tab_id, action, prompt_text)
