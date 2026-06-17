"""Browser automation module using nodriver (pure Python CDP)."""

import json
import shutil
import tempfile
import time
from contextlib import suppress
from pathlib import Path

import nodriver as uc
import nodriver.cdp.console as cdp_console
import nodriver.cdp.debugger as cdp_debugger
import nodriver.cdp.dom_debugger as cdp_dom
import nodriver.cdp.page as cdp_page
import nodriver.cdp.runtime as cdp_runtime

_BASE_PROFILE_DIR = Path.home() / ".config" / "odda" / "chrome-profile"


def _prepare_user_data_dir() -> str | None:
    """Copy base profile to a temp directory, or return None to fall back.

    Checks for a base profile at ~/.config/odda/chrome-profile/.
    If it exists, creates a temp directory and copies the profile into it.
    Returns the temp directory path, or None if the base profile is
    missing or the copy fails.
    """
    if not _BASE_PROFILE_DIR.is_dir():
        return None
    try:
        temp_dir = tempfile.mkdtemp(prefix="odda_")
        shutil.copytree(_BASE_PROFILE_DIR, temp_dir, dirs_exist_ok=True)
    except OSError:
        return None
    else:
        return temp_dir


class BrowserInstance:
    """Manages a single Chrome browser process and its state."""

    def __init__(
        self,
        browser_id: int,
        browser: uc.Browser,
        tab: uc.Tab,
    ):
        """Initialize a browser instance.

        Args:
            browser_id: Unique ID for this browser.
            browser: nodriver Browser object.
            tab: Initial active tab.
        """
        self.browser_id = browser_id
        self.browser = browser
        self.tab = tab
        self._script_map: dict[str, str] = {}
        self._console_buffer: list[dict] = []
        self._console_max = 1000
        self._console_enabled_tabs: set[str] = set()

    async def _close(self) -> None:
        """Close the browser process and reset internal state."""
        with suppress(Exception):
            if self.browser:
                self.browser.stop()
        self.browser = None
        self.tab = None
        self._script_map.clear()
        self._console_buffer.clear()
        self._console_enabled_tabs.clear()

    async def _inject_dialog_override(self) -> None:
        """Inject alert/confirm/prompt overrides via CDP."""
        script_path = Path(__file__).with_name("dialog_override.js")
        source = script_path.read_text()
        await self.tab.send(
            cdp_page.add_script_to_evaluate_on_new_document(source=source)
        )

    async def _enable_debugger(self) -> None:
        """Enable CDP Debugger, Page, and Console domains on the current tab."""
        await self.tab.send(cdp_debugger.enable())
        await self.tab.send(cdp_page.enable())
        self.tab.add_handler(
            cdp_debugger.ScriptParsed,
            self._on_script_parsed,
        )
        self.tab.add_handler(
            cdp_page.FrameNavigated,
            self._on_frame_navigated,
        )
        await self._enable_console()
        await self._inject_dialog_override()

    async def _enable_console(self) -> None:
        """Enable CDP Console domain and register handler on current tab."""
        target_id = self.tab.target.target_id
        if target_id in self._console_enabled_tabs:
            return
        await self.tab.send(cdp_console.enable())
        self.tab.add_handler(
            cdp_console.MessageAdded,
            self._on_console_message,
        )
        self._console_enabled_tabs.add(target_id)

    def _on_script_parsed(self, event: cdp_debugger.ScriptParsed) -> None:
        """Store mapping from script ID to URL."""
        self._script_map[event.script_id] = event.url

    def _on_frame_navigated(self, event: cdp_page.FrameNavigated) -> None:
        """Clear script map and console buffer on main-frame navigation."""
        if event.frame.parent_id is None:
            self._script_map.clear()
            self._console_buffer.clear()

    def _on_console_message(self, event: cdp_console.MessageAdded) -> None:
        """Append a console message to the ring buffer."""
        msg = event.message
        entry = {
            "source": msg.source,
            "level": msg.level,
            "text": msg.text,
            "url": msg.url or "",
            "line": msg.line or 0,
            "column": msg.column or 0,
        }
        self._console_buffer.append(entry)
        if len(self._console_buffer) > self._console_max:
            self._console_buffer.pop(0)

    async def _listeners_for_target(self, tag: str, expr: str) -> list[dict]:
        """Return event listeners for a given target expression."""
        obj, exc = await self.tab.send(cdp_runtime.evaluate(expression=expr))
        if exc or not obj or not obj.object_id:
            return []

        listeners = await self.tab.send(
            cdp_dom.get_event_listeners(object_id=obj.object_id)
        )

        results = [
            {
                "type": listener.type_,
                "element_tag": tag,
                "line_number": listener.line_number,
                "column_number": listener.column_number,
                "script_url": self._script_map.get(listener.script_id),
            }
            for listener in listeners
        ]

        await self.tab.send(cdp_runtime.release_object(object_id=obj.object_id))
        return results

    async def list_event_listeners(self) -> list[dict]:
        """List JavaScript event listeners on window and document."""
        results = []
        for tag, expr in [("window", "window"), ("document", "document")]:
            target_listeners = await self._listeners_for_target(tag, expr)
            results.extend(target_listeners)
        return results

    def list_tabs(self) -> list[dict]:
        """List all open tabs in this browser instance."""
        tabs = []
        for i, tab in enumerate(self.browser):
            tabs.append(
                {
                    "index": i,
                    "url": tab.target.url if tab.target else None,
                    "title": tab.target.title if tab.target else None,
                }
            )
        return tabs

    async def switch_tab(self, index: int) -> str:
        """Switch to a specific tab by index within this browser.

        Returns:
            Status message.
        """
        try:
            tab = self.browser[index]
        except IndexError:
            return f"Tab index {index} not found."

        old = self.tab.target.target_id if self.tab and self.tab.target else None
        if old and old in self._console_enabled_tabs:
            self.tab.remove_handler(cdp_console.MessageAdded, self._on_console_message)
            self._console_enabled_tabs.discard(old)

        self.tab = tab
        await tab.activate()
        self._console_buffer.clear()
        await self._enable_console()
        await self._inject_dialog_override()
        title = tab.target.title if tab.target else None
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
                old = (
                    self.tab.target.target_id if self.tab and self.tab.target else None
                )
                if old and old in self._console_enabled_tabs:
                    self.tab.remove_handler(
                        cdp_console.MessageAdded, self._on_console_message
                    )
                    self._console_enabled_tabs.discard(old)
                self.tab = await self.browser.get(url, new_tab=True)
                await self._enable_debugger()
            else:
                await self.tab.get(url)
            await self.tab
        except Exception as e:
            return f"Failed to navigate: {e!s}"
        else:
            return f"Navigated to: {url}"

    async def eval_js(self, js_code: str) -> str:
        """Execute JavaScript in the active tab.

        Returns:
            JSON-serializable primitives only. Complex platform objects
            return null — use a primitive property (.href, .toString()).
            Promises auto-resolve.
        """
        try:
            result = await self.tab.evaluate(js_code, await_promise=True)

            if isinstance(result, cdp_runtime.ExceptionDetails):
                msg = result.text
                if result.stack_trace:
                    msg += f"\n{result.stack_trace}"
                return f"JavaScript error: {msg}"

            if hasattr(result, "value"):
                result = result.value

            return json.dumps(result)

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
            await self.tab.save_screenshot(filename=str(temp_path))
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
            source: Optional filter by log source.

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
    """Manages multiple Chrome browser instances via CDP using nodriver."""

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
        """Ensure an active browser exists, optionally auto-opening one.

        Args:
            auto_open: If True, automatically open a browser if none active.

        Returns:
            The active BrowserInstance.

        Raises:
            RuntimeError: If no browser is active and auto_open is False.
        """
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

        browser_args = [
            "--no-first-run",
            "--no-default-browser-check",
        ]

        if self.proxy:
            proxy_host = self.proxy.options.listen_host
            proxy_port = self.proxy.options.listen_port
            browser_args.append(f"--proxy-server=http://{proxy_host}:{proxy_port}")
            browser_args.append("--ignore-certificate-errors")
            browser_args.append("--ignore-certificate-errors-spki-list")

        user_data_dir = _prepare_user_data_dir()

        config = uc.Config(
            headless=False,
            sandbox=False,
            browser_args=browser_args,
            lang="en-US",
            host=None,
            user_data_dir=user_data_dir,
        )

        browser = await uc.start(config)
        tab = await browser.get("about:blank")

        instance = BrowserInstance(browser_id, browser, tab)
        await instance._enable_debugger()  # noqa: SLF001

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
        else:
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
        """List all open browser tabs grouped by browser.

        Returns:
            List of dicts with ``browser_id`` and ``tabs`` (list of tab
            dicts with index, url, title).
        """
        await self._ensure_browser()
        result = []
        for browser_id, instance in sorted(self._instances.items()):
            result.append(
                {
                    "browser_id": browser_id,
                    "tabs": instance.list_tabs(),
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

    async def eval_js(self, js_code: str) -> str:
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
