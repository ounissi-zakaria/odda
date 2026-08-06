"""JSON-RPC server that owns browser, proxy, and flow state."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
from pathlib import Path
from typing import Any

from odda import flowstore, proxyscript, rpc
from odda.browser import (
    NAVIGATE_WAIT_UNTIL_EVENTS,
    BrowserManager,
    BrowserOperationError,
)
from odda.proxy import ProxyServer
from odda.request import (
    clone as clone_request,
    new as new_request,
    send as send_request,
    send_pipeline as send_request_pipeline,
)

# Two or more names selects the multi-name pipeline mode (ADR-0019).
_PIPELINE_MIN_NAMES = 2


class OddaServer:
    """Long-running stateful server exposed over a Unix socket."""

    def __init__(
        self,
        socket_path: str | Path,
        data_dir: str | Path,
        parent_pid: int | None = None,
        log_path: str | Path | None = None,
    ) -> None:
        """Initialize server configuration.

        Args:
            socket_path: Unix socket path to listen on.
            data_dir: Directory for flows.jsonl, per-flow dirs, and requests.
                Created lazily on first state write, not at boot.
            parent_pid: Optional parent PID to watch for auto-shutdown.
            log_path: File to append server logs to. ``None`` (default) logs
                to stderr; the caller redirects if a file is desired. Never
                creates the data directory.
        """
        self.socket_path = Path(socket_path)
        self.data_dir = Path(data_dir)
        self.log_path = Path(log_path) if log_path is not None else None
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
        # Re-add any proxy-scripts persisted from a previous server run.
        # Called after set_data_dir so the manager sees the right .odda
        # path, and after ProxyServer is up so the DumpMaster addon chain
        # is ready. A failing proxy-script is logged and skipped, never
        # blocking boot.
        self.proxy.restore_scripts_on_boot()

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError as exc:
                logger.warning("Failed to remove stale socket: %s", exc)

        server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.socket_path),
            limit=rpc.TRANSPORT_LIMIT,
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
        """Configure logging to the configured log destination.

        If ``self.log_path`` is set, appends to that file (creating its
        parent directory if needed — that parent is never the data dir).
        Otherwise logs to stderr. Never creates the data directory.
        """
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            handler: logging.Handler = logging.FileHandler(self.log_path, mode="a")
        else:
            handler = logging.StreamHandler()
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[handler],
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
            response: dict[str, Any] | None = None
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

            # Encode and write inside a guard so an encoding failure (or a
            # transport error) never silently drops the connection. If even
            # this fails, log and bail out of the loop rather than spin.
            try:
                writer.write(rpc.encode(response))
                await writer.drain()
            except Exception as exc:  # pragma: no cover
                logging.getLogger(__name__).warning(
                    "Failed to write response to client: %s", exc
                )
                break

        writer.close()
        await writer.wait_closed()

    # --- JSON-RPC method handlers ---

    async def method_status(self, _params: dict[str, Any]) -> dict[str, Any]:
        """Return server status."""
        return {
            "socket": str(self.socket_path),
            "data_dir": str(self.data_dir),
            "log_path": str(self.log_path) if self.log_path is not None else "",
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
        return await self.browser.open(headless=params.get("headless", True))

    async def method_browser_close(self, params: dict[str, Any]) -> dict[str, Any]:
        """Close a browser instance by ID.

        Returns:
            Dict with browser_id and status.
        """
        return await self.browser.close_instance(params["id"])

    async def method_browser_list(
        self, _params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List every tracked browser with its tab count.

        Returns:
            ``[{browser_id, tab_count}]`` for each browser in
            ``BrowserManager._instances``.
        """
        return self.browser.list_instances()

    async def method_navigate(self, params: dict[str, Any]) -> dict[str, Any]:
        """Navigate an existing tab to a URL.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
            url: URL to navigate to.
            timeout: Optional ``page.goto`` timeout in seconds (default 30).
            wait_until: Optional Playwright lifecycle event to wait for —
                one of ``commit``, ``domcontentloaded``, ``load``,
                ``networkidle`` (default ``load``).
        """
        wait_until = params.get("wait_until", "load")
        if wait_until not in NAVIGATE_WAIT_UNTIL_EVENTS:
            raise rpc.JsonRpcError(
                rpc.INVALID_PARAMS,
                f"wait_until must be one of {', '.join(NAVIGATE_WAIT_UNTIL_EVENTS)}, "
                f"got {wait_until!r}",
            )
        timeout_s = float(params.get("timeout", 30.0))
        return await self.browser.navigate(
            params["browser_id"],
            params["tab_id"],
            params["url"],
            timeout_ms=timeout_s * 1000,
            wait_until=wait_until,
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
            output: Optional path to write the JPEG to. When omitted,
                a temp file is generated.
        """
        return await self.browser.screenshot(
            params["browser_id"], params["tab_id"], params.get("output")
        )

    # --- Page interaction handlers ---

    async def method_page_snapshot(self, params: dict[str, Any]) -> str:
        """Return the page's accessibility tree as agent-readable text.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
        """
        return await self.browser.page_snapshot(params["browser_id"], params["tab_id"])

    async def method_page_click(self, params: dict[str, Any]) -> dict[str, Any]:
        """Click the element identified by ``ref`` in the target tab.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
            ref: Element ref from a snapshot (e.g. ``e2`` or ``f1e2``).
            timeout: Timeout in seconds (default 5).
        """
        timeout_s = float(params.get("timeout", 5.0))
        return await self.browser.page_click(
            params["browser_id"],
            params["tab_id"],
            params["ref"],
            timeout_ms=timeout_s * 1000,
        )

    async def method_page_fill(self, params: dict[str, Any]) -> dict[str, Any]:
        """Fill the element identified by ``ref`` with ``value``.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
            ref: Element ref from a snapshot.
            value: Value to fill (string for text inputs, ``"true"``/
                ``"false"`` for checkboxes/radios).
            timeout: Timeout in seconds (default 5).
        """
        timeout_s = float(params.get("timeout", 5.0))
        return await self.browser.page_fill(
            params["browser_id"],
            params["tab_id"],
            params["ref"],
            params["value"],
            timeout_ms=timeout_s * 1000,
        )

    async def method_page_hover(self, params: dict[str, Any]) -> dict[str, Any]:
        """Hover the element identified by ``ref`` in the target tab.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
            ref: Element ref from a snapshot.
            timeout: Timeout in seconds (default 5).
        """
        timeout_s = float(params.get("timeout", 5.0))
        return await self.browser.page_hover(
            params["browser_id"],
            params["tab_id"],
            params["ref"],
            timeout_ms=timeout_s * 1000,
        )

    async def method_page_upload(self, params: dict[str, Any]) -> dict[str, Any]:
        """Set files on a file input identified by ``ref``.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
            ref: Element ref from a snapshot (the file input element).
            files: List of local file paths to upload.
            timeout: Timeout in seconds (default 5).
        """
        timeout_s = float(params.get("timeout", 5.0))
        return await self.browser.page_upload(
            params["browser_id"],
            params["tab_id"],
            params["ref"],
            params["files"],
            timeout_ms=timeout_s * 1000,
        )

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

    # --- Coverage handlers ---

    async def method_coverage_start(self, params: dict[str, Any]) -> dict[str, Any]:
        """Enable precise block-level coverage on the target tab.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
        """
        return await self.browser.coverage_start(params["browser_id"], params["tab_id"])

    async def method_coverage_snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        """Read per-block hit counts on the target tab without stopping.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
        """
        return await self.browser.coverage_snapshot(
            params["browser_id"], params["tab_id"]
        )

    async def method_coverage_stop(self, params: dict[str, Any]) -> dict[str, Any]:
        """Take a final coverage snapshot and stop recording on the target tab.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
        """
        return await self.browser.coverage_stop(params["browser_id"], params["tab_id"])

    # --- Wrap handlers ---

    async def method_wrap_calls_add(self, params: dict[str, Any]) -> dict[str, Any]:
        """Install a call wrap on a named function.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID (validated for targeting; the wrap
                applies to all tabs via the userscript extension).
            name: Wrap name (used in records and as the userscript
                name suffix).
            expr: JS expression resolving to the function to wrap
                (e.g. ``JSON.parse``).
        """
        return await self.browser.wrap_calls_add(
            params["browser_id"],
            params["tab_id"],
            params["name"],
            params["expr"],
        )

    async def method_wrap_access_add(self, params: dict[str, Any]) -> dict[str, Any]:
        """Install an access wrap on a property accessor.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
            name: Wrap name.
            expr: Dotted JS path to the property (e.g.
                ``document.cookie``).
        """
        return await self.browser.wrap_access_add(
            params["browser_id"],
            params["tab_id"],
            params["name"],
            params["expr"],
        )

    async def method_wrap_list(self, params: dict[str, Any]) -> list[dict]:
        """List installed wraps.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID (validated for targeting).
        """
        return await self.browser.wrap_list(params["browser_id"], params["tab_id"])

    async def method_wrap_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        """Remove a wrap's userscript and reload the extension.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
            name: Wrap name to remove.
        """
        return await self.browser.wrap_remove(
            params["browser_id"], params["tab_id"], params["name"]
        )

    async def method_wrap_dump(self, params: dict[str, Any]) -> list[dict]:
        """Read the per-tab wrap record array.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
            name: Optional wrap name to filter to (server-side, after read).
        """
        return await self.browser.wrap_dump(
            params["browser_id"], params["tab_id"], params.get("name")
        )

    async def method_wrap_clear(self, params: dict[str, Any]) -> dict[str, Any]:
        """Zero the per-tab wrap record array without navigating.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
        """
        return await self.browser.wrap_clear(params["browser_id"], params["tab_id"])

    # --- Logpoint handlers ---

    async def method_logpoint_add(self, params: dict[str, Any]) -> dict[str, Any]:
        """Plant a non-pausing logpoint at a source location.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
            url: Script URL to bind the logpoint to.
            line: 0-based line number.
            col: 0-based column number.
            expr: JS expression to evaluate at each hit.
        """
        return await self.browser.logpoint_add(
            params["browser_id"],
            params["tab_id"],
            params["url"],
            params["line"],
            params["col"],
            params["expr"],
        )

    async def method_logpoint_list(self, params: dict[str, Any]) -> list[dict]:
        """List planted logpoints on the target tab.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
        """
        return await self.browser.logpoint_list(params["browser_id"], params["tab_id"])

    async def method_logpoint_dump(self, params: dict[str, Any]) -> list[dict]:
        """Read the per-tab logpoint record array.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
        """
        return await self.browser.logpoint_dump(params["browser_id"], params["tab_id"])

    async def method_logpoint_clear(self, params: dict[str, Any]) -> dict[str, Any]:
        """Zero the per-tab logpoint record array without navigating.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
        """
        return await self.browser.logpoint_clear(params["browser_id"], params["tab_id"])

    async def method_logpoint_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        """Remove a logpoint's CDP logpoint and registry entry.

        Params:
            browser_id: Target browser ID.
            tab_id: Target tab ID.
            id: Logpoint id (``lp-<n>``) returned by ``logpoint/add``.
        """
        return await self.browser.logpoint_remove(
            params["browser_id"],
            params["tab_id"],
            params["id"],
        )

    # --- Userscript handlers ---

    async def method_userscript_install(self, params: dict[str, Any]) -> dict[str, Any]:
        """Install a userscript into the given browser's scope.

        Params:
            browser_id: Browser whose scope to install into (and reload).
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

    async def method_userscript_list(self, params: dict[str, Any]) -> list[dict]:
        """List installed userscripts for one browser.

        Params:
            browser_id: Browser whose scope to list.
        """
        return self.browser.list_userscripts(params["browser_id"])

    async def method_userscript_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        """Remove a userscript from the given browser's scope.

        Params:
            browser_id: Browser whose scope to remove from (and reload).
            name: Userscript name.
        """
        return await self.browser.remove_userscript(
            params["browser_id"], params["name"]
        )

    # --- Proxy-script handlers ---

    async def method_proxy_script_install(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Install a proxy-script (user-supplied mitmproxy addon).

        Params:
            name: Proxy-script name (odda's key; overwrite gated by ``force``).
            file: Path to a .py file (read by the server), or
            source: Inline Python source. ``file`` takes precedence.
            force: If False (default), refuse when ``name`` is already
                installed; if True, remove the existing instance first.
        """
        name = params["name"]
        file_path = params.get("file")
        if file_path:
            source = Path(file_path).read_text(encoding="utf-8")
        else:
            source = params.get("source", "")
        if not source.strip():
            raise rpc.JsonRpcError(rpc.INVALID_PARAMS, "source is empty")
        force = bool(params.get("force", False))
        if not force and proxyscript.name_exists(name):
            raise rpc.JsonRpcError(
                rpc.INVALID_PARAMS,
                f"Proxy-script '{name}' already installed. Use --force to overwrite.",
            )
        return self.proxy.install_script(name, source)

    async def method_proxy_script_list(self, _params: dict[str, Any]) -> list[dict]:
        """List installed proxy-scripts (from disk, annotated with live state)."""
        return self.proxy.list_scripts()

    async def method_proxy_script_remove(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Remove a proxy-script: live instance + on-disk source.

        Params:
            name: Proxy-script name.
        """
        return self.proxy.remove_script(params["name"])

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

    async def method_request_send(self, params: dict[str, Any]) -> Any:
        """Send an editable request and record the response as a flow.

        Single-name (``name``) is the frozen single-shot contract: one
        flow record, one ``--json`` object. Multi-name (``names`` as a
        list of two or more) is the pipeline mode: one HTTP/1.1
        connection, one flow record per request, a list of records in
        ``--json``. See ADR-0019.

        Params:
            name: Editable request name (single-name mode).
            names: List of editable request names (multi-name pipeline
                mode). When present, ``name`` is ignored.
            fix_content_length: Recompute Content-Length from the body
                before sending (single-name only; rejected in multi-name).
            timeout: Total timeout in seconds (default 30).
            insecure: Skip TLS certificate verification (default False).
            pipelining: Send all requests then read all responses (true
                H1 pipelining) instead of send-then-read per request
                (sequential keep-alive, the default). Multi-name only.
        """
        names = params.get("names")
        if isinstance(names, list) and len(names) >= _PIPELINE_MIN_NAMES:
            return await send_request_pipeline(
                names,
                fix_content_length=params.get("fix_content_length", False),
                timeout=float(params.get("timeout", 30.0)),
                insecure=params.get("insecure", False),
                pipelining=params.get("pipelining", False),
            )
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
    log_path: str | Path | None = None,
) -> None:
    """Run the odda server.

    Args:
        socket_path: Unix socket path.
        data_dir: Data directory. Created lazily on first state write.
        parent_pid: Optional parent PID to watch.
        log_path: File to append logs to. ``None`` logs to stderr.
    """
    server = OddaServer(socket_path, data_dir, parent_pid, log_path=log_path)
    asyncio.run(server.run())
