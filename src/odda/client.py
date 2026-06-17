"""JSON-RPC client for talking to the odda server."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from odda import rpc


class OddaClientError(Exception):
    """Client-side error connecting to or communicating with the server."""


class OddaClient:
    """Async JSON-RPC client for odda."""

    def __init__(
        self,
        socket_path: str | Path | None = None,
        connect_timeout: float = 5.0,
    ) -> None:
        """Initialize client.

        Args:
            socket_path: Unix socket path.
            connect_timeout: Max seconds to wait for the socket to appear.
        """
        if not socket_path:
            msg = "No odda server socket configured. Pass --socket or set ODDA_SOCKET."
            raise OddaClientError(msg)
        self.socket_path = Path(socket_path)
        self.connect_timeout = connect_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._request_id = 0

    async def _connect(self) -> None:
        """Connect to the server, waiting briefly if the socket is not ready."""
        deadline = time.time() + self.connect_timeout
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(
                    str(self.socket_path)
                )
            except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                last_error = exc
                await asyncio.sleep(0.05)
            else:
                return
        msg = f"Could not connect to odda server at {self.socket_path}: {last_error}"
        raise OddaClientError(msg)

    async def _ensure_connected(self) -> None:
        """Ensure a connection is open."""
        if self._writer is None or self._reader is None:
            await self._connect()

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Call a JSON-RPC method and return the result.

        Args:
            method: Method name.
            params: Method parameters.

        Returns:
            Parsed result.

        Raises:
            OddaClientError: If the server returns an error or connection fails.
        """
        await self._ensure_connected()
        self._request_id += 1
        request_id = self._request_id
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        self._writer.write(rpc.encode(message))
        await self._writer.drain()

        while True:
            line = await self._reader.readline()
            if not line:
                raise OddaClientError("Server closed connection")
            try:
                response = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise OddaClientError(f"Invalid server response: {exc}") from exc

            if response.get("id") == request_id:
                if "error" in response:
                    raise OddaClientError(
                        f"Server error ({response['error']['code']}): "
                        f"{response['error']['message']}"
                    )
                return response["result"]
