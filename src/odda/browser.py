"""Browser automation module using patchright (Playwright)."""

import shutil
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from patchright.async_api import (
    BrowserContext,
    CDPSession,
    ConsoleMessage,
    Page,
    Playwright,
    async_playwright,
)

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


class BrowserInstance:
    """Manages a single Chrome browser context and its active page."""

    def __init__(
        self,
        browser_id: int,
        playwright: Playwright,
        context: BrowserContext,
        page: Page,
    ) -> None:
        """Initialize a browser instance.

        Args:
            browser_id: Unique ID for this browser.
            playwright: Patchright Playwright object.
            context: BrowserContext (persistent context for this browser).
            page: Initial active page.
        """
        self.browser_id = browser_id
        self.playwright = playwright
        self.context = context
        self.page = page
        self._console_buffer: list[dict] = []
        self._console_max = 1000
        self._script_map: dict[str, str] = {}
        self._cdp_session: CDPSession | None = None
        self._cdp_setup_done = False

        self._setup_page_handlers(self.page)

    def _setup_page_handlers(self, page: Page) -> None:
        """Attach console and navigation handlers to a page."""
        page.on("console", self._on_console_message)
        page.on("framenavigated", self._on_frame_navigated)

    async def _ensure_cdp_session(self) -> CDPSession:
        """Create and configure a CDP session for the active page."""
        if self._cdp_session is None:
            self._cdp_session = await self.context.new_cdp_session(self.page)
            await self._cdp_session.send("Runtime.enable")
            await self._cdp_session.send("Debugger.enable")
            self._cdp_session.on(
                "Debugger.scriptParsed",
                self._on_script_parsed,
            )
        return self._cdp_session

    async def _close(self) -> None:
        """Close the browser context and release resources."""
        with suppress(Exception):
            if self.context:
                await self.context.close()
        with suppress(Exception):
            if self.playwright:
                await self.playwright.stop()
        self.context = None  # type: ignore[assignment]
        self.page = None  # type: ignore[assignment]
        self.playwright = None  # type: ignore[assignment]
        self._cdp_session = None
        self._console_buffer.clear()
        self._script_map.clear()

    def _on_script_parsed(self, event: dict[str, Any]) -> None:
        """Store mapping from script ID to URL."""
        self._script_map[event.get("scriptId", "")] = event.get("url", "")

    def _on_frame_navigated(self, frame) -> None:
        """Clear script map and console buffer on main-frame navigation."""
        if frame.parent_frame is None:
            self._script_map.clear()
            self._console_buffer.clear()

    def _on_console_message(self, msg: ConsoleMessage) -> None:
        """Append a console message to the ring buffer."""
        location = msg.location or {}
        entry = {
            "source": "console-api",
            "level": msg.type,
            "text": msg.text,
            "url": location.get("url", ""),
            "line": location.get("lineNumber", 0),
            "column": location.get("columnNumber", 0),
        }
        self._console_buffer.append(entry)
        if len(self._console_buffer) > self._console_max:
            self._console_buffer.pop(0)

    async def _listeners_for_target(self, tag: str, expr: str) -> list[dict]:
        """Return event listeners for a given target expression via CDP."""
        cdp = await self._ensure_cdp_session()

        result = await cdp.send(
            "Runtime.evaluate",
            {
                "expression": expr,
                "objectGroup": "odda-event-listeners",
                "includeCommandLineAPI": False,
            },
        )
        obj = result.get("result", {})
        if not obj or not obj.get("objectId"):
            return []

        listeners_result = await cdp.send(
            "DOMDebugger.getEventListeners",
            {"objectId": obj["objectId"]},
        )
        listeners = listeners_result.get("listeners", [])

        await cdp.send("Runtime.releaseObject", {"objectId": obj["objectId"]})

        return [
            {
                "type": listener.get("type", ""),
                "element_tag": tag,
                "line_number": listener.get("lineNumber"),
                "column_number": listener.get("columnNumber"),
                "script_url": self._script_map.get(listener.get("scriptId")),
            }
            for listener in listeners
        ]

    async def list_event_listeners(self) -> list[dict]:
        """List JavaScript event listeners on window and document."""
        results = []
        for tag, expr in [("window", "window"), ("document", "document")]:
            target_listeners = await self._listeners_for_target(tag, expr)
            results.extend(target_listeners)
        return results

    async def list_tabs(self) -> list[dict]:
        """List all open tabs (pages) in this browser context."""
        tabs = []
        for i, page in enumerate(self.context.pages):
            title = ""
            with suppress(Exception):
                title = await page.title()
            tabs.append(
                {
                    "index": i,
                    "url": page.url,
                    "title": title,
                }
            )
        return tabs

    async def switch_tab(self, index: int) -> str:
        """Switch to a specific tab by index within this browser.

        Returns:
            Status message.
        """
        pages = self.context.pages
        if index < 0 or index >= len(pages):
            return f"Tab index {index} not found."

        self.page = pages[index]
        with suppress(Exception):
            await self.page.bring_to_front()
        self._console_buffer.clear()

        title = ""
        with suppress(Exception):
            title = await self.page.title()
        return f"Switched to tab {index}: {title}"

    async def navigate(self, url: str, *, new_tab: bool = False) -> str:
        """Navigate browser to URL.

        Args:
            url: URL to navigate to.
            new_tab: If True, open URL in a new tab.

        Returns:
            Status message.
        """
        try:
            if new_tab:
                self.page = await self.context.new_page()
                self._setup_page_handlers(self.page)
                await self.page.goto(url)
            else:
                await self.page.goto(url)
        except Exception as e:
            return f"Failed to navigate: {e!s}"
        return f"Navigated to: {url}"

    async def eval_js(self, js_code: str) -> Any:
        """Execute JavaScript in the active tab.

        Returns:
            Result of the evaluation (Playwright handles promise resolution).
        """
        try:
            return await self.page.evaluate(js_code)
        except Exception as e:
            return f"JavaScript error: {e!s}"

    async def screenshot(self) -> str:
        """Capture screenshot of the current viewport.

        Returns:
            Path to saved screenshot JPEG file.
        """
        try:
            temp_dir = Path(tempfile.gettempdir())
            temp_path = temp_dir / f"screenshot_{int(time.time())}.jpeg"
            await self.page.screenshot(
                path=str(temp_path),
                type="jpeg",
                full_page=False,
            )
            return str(temp_path)
        except Exception as e:
            return f"Screenshot error: {e!s}"

    async def read_console(
        self, n: int = 50, level: str | None = None, source: str | None = None
    ) -> list[dict]:
        """Return recent console messages from the ring buffer.

        Args:
            n: Maximum number of messages to return (default: 50).
            level: Optional filter by log level.
            source: Optional filter by source.

        Returns:
            List of message dicts.
        """
        messages = self._console_buffer
        if level:
            messages = [m for m in messages if m["level"] == level]
        if source:
            messages = [m for m in messages if m["source"] == source]
        return messages[-n:]


class BrowserManager:
    """Manages multiple Chrome browser instances via patchright."""

    def __init__(self, proxy=None):
        """Initialize browser manager.

        Args:
            proxy: Proxy server instance to share across browsers.
        """
        self._instances: dict[int, BrowserInstance] = {}
        self._active_browser_id: int | None = None
        self._next_id: int = 1
        self.proxy = proxy

    @property
    def browser_count(self) -> int:
        """Return the number of open browser instances."""
        return len(self._instances)

    def list_instances(self) -> list[dict]:
        """List open browser instances with active flag."""
        return [
            {
                "browser_id": bid,
                "active": bid == self._active_browser_id,
            }
            for bid in sorted(self._instances.keys())
        ]

    async def _ensure_browser(self, *, auto_open: bool = False) -> BrowserInstance:
        """Ensure an active browser exists, optionally auto-opening one."""
        if self._active_browser_id is not None:
            return self._instances[self._active_browser_id]
        if auto_open:
            return await self._create_instance()
        raise RuntimeError(
            "No browser open. Navigate to a URL using navigate or open_browser first."
        )

    async def _create_instance(self) -> BrowserInstance:
        """Create and register a new BrowserInstance."""
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
            headless=False,
            proxy=proxy_config,
            ignore_https_errors=True,
            args=[
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        instance = BrowserInstance(browser_id, playwright, context, page)
        self._instances[browser_id] = instance
        self._active_browser_id = browser_id
        return instance

    async def open(self) -> str:
        """Open a new Chrome browser window.

        Returns:
            Status message including the new browser ID.
        """
        try:
            instance = await self._create_instance()
        except Exception as e:
            return f"Failed to open browser: {e!s}"
        return f"Browser {instance.browser_id} launched"

    async def close_instance(self, browser_id: int) -> str:
        """Close a browser instance.

        Args:
            browser_id: ID of the browser to close.

        Returns:
            Status message.
        """
        if browser_id not in self._instances:
            return f"Browser {browser_id} not found."

        instance = self._instances[browser_id]
        await instance._close()  # noqa: SLF001
        del self._instances[browser_id]

        if self._active_browser_id == browser_id:
            if self._instances:
                self._active_browser_id = min(self._instances.keys())
                return (
                    f"Browser {browser_id} closed. "
                    f"Active browser is now {self._active_browser_id}."
                )
            self._active_browser_id = None
            return f"Browser {browser_id} closed. No browsers remaining."

        active_info = ""
        if self._active_browser_id is not None:
            active_info = f" Active browser is {self._active_browser_id}."
        return f"Browser {browser_id} closed.{active_info}"

    async def list_tabs(self) -> list[dict]:
        """List all open browser tabs grouped by browser."""
        await self._ensure_browser()
        result = []
        for browser_id, instance in sorted(self._instances.items()):
            result.append(
                {
                    "browser_id": browser_id,
                    "tabs": await instance.list_tabs(),
                }
            )
        return result

    async def switch_tab(self, browser_id: int, index: int) -> str:
        """Switch to a specific tab in a specific browser.

        Also sets the active browser to the target browser.

        Args:
            browser_id: ID of the target browser.
            index: Tab index within that browser.

        Returns:
            Status message.
        """
        if browser_id not in self._instances:
            return f"Browser {browser_id} not found."

        instance = self._instances[browser_id]
        result = await instance.switch_tab(index)
        if result.startswith("Switched to tab"):
            self._active_browser_id = browser_id
        return result

    async def navigate(self, url: str, *, new_tab: bool = False) -> dict:
        """Navigate active browser to URL.

        Opens a new browser automatically if none is active and reports
        whether a browser was auto-opened.

        Returns:
            Dict with status message and auto_opened flag.
        """
        auto_opened = self._active_browser_id is None
        inst = await self._ensure_browser(auto_open=True)
        status = await inst.navigate(url, new_tab=new_tab)
        return {"status": status, "auto_opened": auto_opened}

    async def eval_js(self, js_code: str) -> Any:
        """Execute JavaScript in the active browser tab."""
        inst = await self._ensure_browser()
        return await inst.eval_js(js_code)

    async def screenshot(self) -> str:
        """Capture screenshot of the active browser viewport."""
        inst = await self._ensure_browser()
        return await inst.screenshot()

    async def list_event_listeners(self) -> list[dict]:
        """List JS event listeners in the active browser tab."""
        inst = await self._ensure_browser()
        return await inst.list_event_listeners()

    async def read_console(
        self, n: int = 50, level: str | None = None, source: str | None = None
    ) -> list[dict]:
        """Return recent console messages from the active browser."""
        inst = await self._ensure_browser()
        return await inst.read_console(n=n, level=level, source=source)
