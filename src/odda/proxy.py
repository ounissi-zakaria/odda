"""Proxy server wrapping mitmproxy with asyncio.create_task."""

from __future__ import annotations

import asyncio
import socket
from contextlib import suppress
from typing import Any

from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster

from odda.flowstore import FlowFileAddon
from odda.proxyscript import ProxyScriptManager


class ProxyServer:
    """Manages mitmproxy server lifecycle - auto-starts on init without blocking."""

    def __init__(
        self,
        host: str = "127.0.0.1",
    ) -> None:
        """Initialize proxy server.

        Args:
            host: Host to bind to.
        """
        self.m = None
        port = self._pick_port(host)
        self.options = Options(
            listen_port=port,
            listen_host=host,
        )
        # odda's audience tests against staging boxes, internal hosts, and
        # appliances with self-signed/expired certs. Skipping upstream TLS
        # validation lets the proxy forward to those hosts without 502s.
        # See docs/adr/0007-proxy-ssl-insecure.md.
        self.options.ssl_insecure = True
        self.m = DumpMaster(self.options, with_termlog=False, with_dumper=False)
        # DumpMaster ships mitmproxy's ErrorCheck addon: any ERROR-level
        # log record during its startup checkpoints sys.exit(1)s the
        # process. That is CLI UX (a human watching a console); odda is a
        # library embedding DumpMaster, and the MCP process is an agent's
        # session — a broken persisted proxy-script (logged, then skipped
        # per its contract) or any stray startup ERROR must not kill the
        # session. Disarm it: out of the addon chain (no checkpoints) and
        # its global log handler uninstalled.
        errorcheck = self.m.addons.get("errorcheck")
        if errorcheck is not None:
            self.m.addons.remove(errorcheck)
            errorcheck.finish()
        # Real-world servers send non-conformant HTTP/2 header values (e.g.
        # ` IE=Edge`); the h2 library rejects those and mitmproxy 502s. Skip
        # inbound header validation to accept them. Set after DumpMaster
        # construction: the option is registered by the proxyserver addon
        # during init, so it is unknown before then.
        # See docs/adr/0012-proxy-skip-inbound-header-validation.md.
        self.options.validate_inbound_headers = False
        # Set client replay concurrency to -1 (no limit) so queue.join()
        # returns after request is dispatched, not after response received.
        self.m.options.client_replay_concurrency = -1

        # Initialize flow storage addon
        self.db_addon = FlowFileAddon()
        self.m.addons.add(self.db_addon)

        # Proxy-script manager: holds user-supplied mitmproxy addons.
        # Constructed after FlowFileAddon so FlowFileAddon stays ahead of
        # user proxy-scripts in the chain — captured .odda/flows/<id>/
        # files record the original request/response and a proxy-script's
        # mutations affect what goes upstream, not what is captured
        # (ADR-0018).
        self.scripts = ProxyScriptManager(self.m)

        self.task = asyncio.create_task(self.m.run())

    @staticmethod
    def _pick_port(host: str) -> int:
        """Bind a socket to get an OS-assigned free port.

        Avoids the TOCTOU race in check-then-bind patterns: we bind a
        socket, read the assigned port, and close it immediately before
        passing it to mitmproxy. The close→bind window is much smaller
        than scanning a range of ports, and in practice two servers
        starting in parallel get different ports from the OS.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, 0))
            return s.getsockname()[1]

    async def shutdown(self) -> None:
        """Shut down the proxy server and wait for the task to finish."""
        self.m.shutdown()
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task

    @property
    def proxy_url(self) -> str:
        """Return the proxy URL for browser configuration.

        Returns:
            HTTP proxy URL string.
        """
        return f"http://{self.options.listen_host}:{self.options.listen_port}"

    # --- proxy-scripts ------------------------------------------------

    def install_script(self, name: str, source: str) -> dict[str, Any]:
        """Persist, exec, and add a proxy-script under ``name``.

        Overwrites an existing proxy-script of the same name (remove-then-add),
        which is the ``--force`` path; the caller gates whether overwrite
        is allowed.

        Raises:
            ValueError: If exec fails (syntax/import error). The persisted
                file is left on disk for the boot scan to retry.
        """
        return self.scripts.install(name, source)

    def remove_script(self, name: str) -> dict[str, Any]:
        """Remove a proxy-script: live instance + on-disk source.

        Raises:
            ValueError: If the proxy-script is not on disk.
        """
        return self.scripts.remove(name)

    def list_scripts(self) -> list[dict[str, Any]]:
        """List proxy-scripts from disk, annotated with whether they're live."""
        return self.scripts.list_live()

    def restore_scripts_on_boot(self) -> None:
        """Re-exec and add every persisted proxy-script.

        Called during server boot, after :func:`flowstore.set_data_dir`.
        A proxy-script that fails to exec is logged and skipped; the rest
        of the chain comes up.
        """
        self.scripts.restore_on_boot()
