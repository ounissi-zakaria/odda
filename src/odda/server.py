"""JSON-RPC server that owns browser, proxy, and flow state."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
from pathlib import Path
from typing import Any

from odda import flowstore, rpc
from odda.browser import BrowserManager, BrowserOperationError
from odda.proxy import ProxyServer
from odda.request import (
    clone as clone_request,
    new as new_request,
    send as send_request,
)


class OddaServer:
    """Long-running stateful server exposed over a Unix socket."""

    def __init__(
        self,
        socket_path: str | Path,
        data_dir: str | Path,
        parent_pid: int | None = None,
    ) -> None:
        """Initialize server configuration.

        Args:
            socket_path: Unix socket path to listen on.
            data_dir: Directory for flows.db, bodies/, and server.log.
            parent_pid: Optional parent PID to watch for auto-shutdown.
        """
        self.socket_path = Path(socket_path)
        self.data_dir = Path(data_dir)
        self.parent_pid = parent_pid
        self.proxy: ProxyServer | None = None
        self.browser: BrowserManager | None = None
        self._shutdown_event = asyncio.Event()
        self._parent_watch_task: asyncio.Task | None = None

    async def run(self) -> None:
        """Start and run the server until shutdown."""
        self._setup_logging()
        logger = logging.getLogger(__name__)
        logger.info("Starting odda server (data_dir=%s)", self.data_dir)

        flowstore.set_data_dir(self.data_dir)
        self.proxy = ProxyServer()
        self.browser = BrowserManager(proxy=self.proxy)

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError as exc:
                logger.warning("Failed to remove stale socket: %s", exc)

        server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.socket_path),
        )
        logger.info("Listening on %s", self.socket_path)

        loop = asyncio.get_running_loop()
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self._signal_handler, sig)

        if self.parent_pid is not None:
            self._parent_watch_task = asyncio.create_task(self._watch_parent())

        try:
            await self._shutdown_event.wait()
        finally:
            logger.info("Shutting down odda server")
            server.close()
            await server.wait_closed()
            await self._cleanup()
            if self.socket_path.exists():
                self.socket_path.unlink()
            logger.info("Shutdown complete")

    def _setup_logging(self) -> None:
        """Configure logging to append to server.log in the data directory."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        log_file = self.data_dir / "server.log"
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[logging.FileHandler(log_file, mode="a")],
        )

    def _signal_handler(self, sig: int) -> None:
        """Handle shutdown signals."""
        logging.getLogger(__name__).info("Received signal %s", sig)
        self._shutdown_event.set()

    async def _watch_parent(self) -> None:
        """Poll parent PID and shutdown if it disappears."""
        logger = logging.getLogger(__name__)
        while True:
            try:
                if self.parent_pid is not None:
                    os.kill(self.parent_pid, 0)
            except ProcessLookupError:
                logger.info("Parent process %s gone; shutting down", self.parent_pid)
                self._shutdown_event.set()
                return
            except PermissionError:
                logger.warning(
                    "No permission to signal parent process %s; continuing",
                    self.parent_pid,
                )
            except OSError:
                logger.exception(
                    "Unexpected error checking parent process %s",
                    self.parent_pid,
                )
            await asyncio.sleep(2)

    async def _cleanup(self) -> None:
        """Close browsers and stop proxy."""
        if self.browser is not None:
            for browser_id in [b["browser_id"] for b in self.browser.list_instances()]:
                await self.browser.close_instance(browser_id)
        if self.proxy is not None:
            await self.proxy.shutdown()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a single JSON-RPC client connection."""
        while True:
            line = await reader.readline()
            if not line:
                break

            request_id: Any = None
            try:
                request = rpc.parse_request(line.decode("utf-8"))
                method_name = request["method"]
                params = request.get("params") or {}
                request_id = request.get("id")
                handler = getattr(
                    self,
                    f"method_{method_name.replace('/', '_').replace('-', '_')}",
                    None,
                )
                if handler is None:
                    raise rpc.JsonRpcError(
                        rpc.METHOD_NOT_FOUND, f"Method not found: {method_name}"
                    )
                if not isinstance(params, dict):
                    raise rpc.JsonRpcError(
                        rpc.INVALID_PARAMS, "Params must be an object"
                    )
                result = await handler(params)
                response = rpc.build_response(request_id, result)
            except rpc.JsonRpcError as exc:
                response = rpc.build_error(request_id, exc.code, exc.message, exc.data)
            except BrowserOperationError as exc:
                response = rpc.build_error(request_id, rpc.INVALID_PARAMS, exc.message)
            except Exception as exc:  # pragma: no cover
                response = rpc.build_error(request_id, rpc.INTERNAL_ERROR, str(exc))

            writer.write(rpc.encode(response))
            await writer.drain()

        writer.close()
        await writer.wait_closed()

    # --- JSON-RPC method handlers ---

    async def method_status(self, _params: dict[str, Any]) -> dict[str, Any]:
        """Return server status."""
        return {
            "socket": str(self.socket_path),
            "data_dir": str(self.data_dir),
            "parent_pid": self.parent_pid,
            "proxy_url": self.proxy.proxy_url if self.proxy else None,
            "browser_count": self.browser.browser_count if self.browser else 0,
        }

    async def method_proxy_url(self, _params: dict[str, Any]) -> str:
        """Return the HTTP proxy URL."""
        if self.proxy is None:
            raise rpc.JsonRpcError(rpc.INTERNAL_ERROR, "Proxy not initialized")
        return self.proxy.proxy_url

    async def method_browser_open(self, params: dict[str, Any]) -> dict[str, Any]:
        """Open a new browser instance.

        Returns:
            Dict with browser_id, the initial tab_id, and status.
        """
        return await self.browser.open(headless=params.get("headless", False))

    async def method_browser_close(self, params: dict[str, Any]) -> dict[str, Any]:
        """Close a browser instance by ID.

        Returns:
            Dict with browser_id and status.
        """
        return await self.browser.close_instance(params["id"])

    async def method_navigate(self, params: dict[str, Any]) -> dict[str, Any]:
        """Navigate an existing tab to a URL.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
            url: URL to navigate to.
        """
        return await self.browser.navigate(
            params["browser_id"], params["tab_id"], params["url"]
        )

    async def method_eval(self, params: dict[str, Any]) -> Any:
        """Evaluate JavaScript in the target tab.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
            js: JavaScript code to execute.
        """
        return await self.browser.eval_js(
            params["browser_id"], params["tab_id"], params["js"]
        )

    async def method_wait_for(self, params: dict[str, Any]) -> Any:
        """Poll a JS expression until truthy or timeout.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
            expression: JS expression to poll.
            timeout: Timeout in seconds (default 30).
        """
        timeout_s = float(params.get("timeout", 30.0))
        return await self.browser.wait_for(
            params["browser_id"],
            params["tab_id"],
            params["expression"],
            timeout_ms=timeout_s * 1000,
        )

    async def method_screenshot(self, params: dict[str, Any]) -> str:
        """Capture a screenshot of the target tab's viewport.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
        """
        return await self.browser.screenshot(params["browser_id"], params["tab_id"])

    async def method_tabs_list(self, params: dict[str, Any]) -> list[dict]:
        """List tabs grouped by browser.

        Params:
            browser_id: Optional filter to one browser.
        """
        browser_id = params.get("browser_id")
        return await self.browser.list_tabs(browser_id)

    async def method_tabs_open(self, params: dict[str, Any]) -> dict[str, Any]:
        """Open a new tab in a browser, optionally navigating to a URL.

        Params:
            browser_id: Target browser ID.
            url: Optional URL to navigate the new tab to (about:blank if
                omitted).
        """
        return await self.browser.open_tab(params["browser_id"], params.get("url"))

    async def method_tabs_close(self, params: dict[str, Any]) -> dict[str, Any]:
        """Close a tab in a browser.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
        """
        return await self.browser.close_tab(params["browser_id"], params["tab_id"])

    async def method_event_listeners(self, params: dict[str, Any]) -> list[dict]:
        """List JS event listeners on window and document in the target tab.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
        """
        return await self.browser.list_event_listeners(
            params["browser_id"], params["tab_id"]
        )

    # --- Userscript handlers ---

    async def method_userscript_install(self, params: dict[str, Any]) -> dict[str, Any]:
        """Install a userscript from a file or inline source.

        Params:
            browser_id: Browser to reload the extension on.
            name: Userscript name.
            file: Path to a JS file (read by the server), or
            source: Inline JS source. ``file`` takes precedence.
        """
        browser_id = params["browser_id"]
        name = params["name"]
        file_path = params.get("file")
        if file_path:
            source = Path(file_path).read_text(encoding="utf-8")
        else:
            source = params.get("source", "")
        if not source.strip():
            raise rpc.JsonRpcError(rpc.INVALID_PARAMS, "source is empty")
        return await self.browser.install_userscript(browser_id, name, source)

    async def method_userscript_list(self, _params: dict[str, Any]) -> list[dict]:
        """List installed userscripts."""
        return self.browser.list_userscripts()

    async def method_userscript_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        """Remove a userscript.

        Params:
            browser_id: Browser to reload the extension on.
            name: Userscript name.
        """
        return await self.browser.remove_userscript(
            params["browser_id"], params["name"]
        )

    # --- Request (raw resend) handlers ---

    async def method_request_clone(self, params: dict[str, Any]) -> dict[str, Any]:
        """Clone a captured flow's request into an editable request.

        Params:
            flow_id: Flow id (e.g. ``00042``).
            name: Editable request name.
            force: If True, overwrite an existing request of the same name.
        """
        return clone_request(
            params["flow_id"],
            params["name"],
            force=params.get("force", False),
        )

    async def method_request_new(self, params: dict[str, Any]) -> dict[str, Any]:
        """Create a new empty editable request.

        Params:
            name: Editable request name.
            host: Target host (required).
            protocol: ``http`` or ``https`` (default ``https``).
            port: Target port (default 80 for http, 443 for https).
            force: If True, overwrite an existing request of the same name.
        """
        return new_request(
            params["name"],
            host=params["host"],
            protocol=params.get("protocol", "https"),
            port=params.get("port"),
            force=params.get("force", False),
        )

    async def method_request_send(self, params: dict[str, Any]) -> dict[str, Any]:
        """Send an editable request and record the response as a flow.

        Params:
            name: Editable request name.
            fix_content_length: Recompute Content-Length from the body
                before sending (default False).
            timeout: Total timeout in seconds (default 30).
            insecure: Skip TLS certificate verification (default False).
        """
        return await send_request(
            params["name"],
            fix_content_length=params.get("fix_content_length", False),
            timeout=float(params.get("timeout", 30.0)),
            insecure=params.get("insecure", False),
        )


def run(
    socket_path: str | Path,
    data_dir: str | Path,
    parent_pid: int | None = None,
) -> None:
    """Run the odda server.

    Args:
        socket_path: Unix socket path.
        data_dir: Data directory.
        parent_pid: Optional parent PID to watch.
    """
    server = OddaServer(socket_path, data_dir, parent_pid)
    asyncio.run(server.run())
