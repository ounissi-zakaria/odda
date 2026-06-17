"""Proxy server wrapping mitmproxy with asyncio.create_task."""

import asyncio
from contextlib import suppress

from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster

from odda.database import DatabaseAddon
from odda.utils import find_available_port


class ProxyServer:
    """Manages mitmproxy server lifecycle - auto-starts on init without blocking."""

    def __init__(
        self,
        port: int = 38080,
        host: str = "127.0.0.1",
    ) -> None:
        """Initialize proxy server.

        Args:
            port: Port to listen on.
            host: Host to bind to.
        """
        self.m = None
        port = find_available_port(host, port)
        self.options = Options(
            listen_port=port,
            listen_host=host,
        )
        self.loop = asyncio.new_event_loop()
        self.m = DumpMaster(
            self.options, loop=self.loop, with_termlog=False, with_dumper=False
        )
        # Set client replay concurrency to -1 (no limit) so queue.join()
        # returns after request is dispatched, not after response received.
        self.m.options.client_replay_concurrency = -1

        # Initialize database addon for flow storage
        self.db_addon = DatabaseAddon()
        self.m.addons.add(self.db_addon)

        self.task = asyncio.create_task(self.m.run())

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
