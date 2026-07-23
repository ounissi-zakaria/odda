"""Proxy server wrapping mitmproxy with asyncio.create_task."""

from __future__ import annotations

import asyncio
import socket
from contextlib import suppress

from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster

from odda.flowstore import FlowFileAddon


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
