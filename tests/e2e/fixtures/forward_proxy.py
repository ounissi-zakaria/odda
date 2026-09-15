"""Minimal upstream forward-proxy fixture for the upstream-proxy e2e tests.

Accepts both CONNECT (tunnel) and absolute-form (plain HTTP) proxy
requests on 127.0.0.1:<port>, optionally demanding HTTP Basic
authentication (``--require-auth USER:PASS``), and appends one JSON line
per handled request to ``--log PATH`` so tests can assert what the
upstream actually saw:

    {"kind": "connect" | "request" | "reject", "line": "..."}

CONNECT: replies ``200 Connection established`` (after the auth check),
then blind-pipes bytes in both directions until EOF — the tunneled
origin TLS is untouched. Absolute-form: forwards the request head+body
verbatim to the origin, relays the response back (Content-Length or
EOF framing, forced ``Connection: close``), then closes the client
connection.

Run as: python3 forward_proxy.py <port> [--require-auth USER:PASS] --log PATH
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
from pathlib import Path
from urllib.parse import urlsplit

READ_CHUNK = 65536
HEAD_LIMIT = 1 << 20


def log_entry(log_path: Path, kind: str, line: str) -> None:
    """Append one JSON log line; small volume, open-per-write is fine."""
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": kind, "line": line}) + "\n")


def header_value(head: bytes, name: bytes) -> bytes | None:
    """Case-insensitive single-header lookup in a CRLF-joined head."""
    for line in head.split(b"\r\n")[1:]:
        key, _, value = line.partition(b":")
        if key.strip().lower() == name:
            return value.strip()
    return None


async def read_head(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    r"""Read one request/response head; return (head_with_crlfcrlf, excess)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = await reader.read(READ_CHUNK)
        if not chunk:
            return buf, b""
        buf += chunk
        if len(buf) > HEAD_LIMIT:
            raise ValueError("proxy fixture: head exceeds limit")
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head + b"\r\n\r\n", rest


def force_connection_close(head: bytes) -> bytes:
    """Rewrite a response head so Connection: close is stated exactly once."""
    lines = head.split(b"\r\n")
    kept = [
        lines[0],
        *(
            line
            for line in lines[1:]
            if line.partition(b":")[0].strip().lower() != b"connection"
        ),
    ]
    if kept[-1] != b"":
        kept.append(b"Connection: close")
    return b"\r\n".join(kept)


class ForwardProxy:
    """The asyncio upstream-proxy fixture logic."""

    def __init__(self, log_path: Path, expected_b64: bytes | None) -> None:
        """Bind the log sink and the expected Basic credentials (None = open)."""
        self.log_path = log_path
        self.expected_b64 = expected_b64

    def _authorized(self, head: bytes) -> bool:
        """Check the Proxy-Authorization header against the required credential."""
        if self.expected_b64 is None:
            return True
        return header_value(head, b"proxy-authorization") == (
            b"Basic " + self.expected_b64
        )

    async def _reject_407(self, writer: asyncio.StreamWriter, line: str) -> None:
        """Log and answer a rejected request with a bare 407."""
        log_entry(self.log_path, "reject", line)
        writer.write(
            b"HTTP/1.1 407 Proxy Authentication Required\r\n"
            b'Proxy-Authenticate: Basic realm="fp"\r\n'
            b"Content-Length: 0\r\n\r\n"
        )
        await writer.drain()

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Serve one client connection: CONNECT tunnel or absolute-form forwards."""
        try:
            while True:
                head, _ = await read_head(reader)
                if not head.strip():
                    return
                request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
                if request_line.upper().startswith("CONNECT"):
                    await self._handle_connect(reader, writer, head, request_line)
                    return
                if not await self._handle_absolute(reader, writer, head, request_line):
                    return
        except (ConnectionError, TimeoutError, ValueError, OSError):
            pass
        finally:
            with contextlib.suppress(OSError):
                writer.close()

    async def _handle_connect(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        head: bytes,
        request_line: str,
    ) -> None:
        """Authenticate, ack the CONNECT, then blind-pipe the tunnel to EOF."""
        if not self._authorized(head):
            await self._reject_407(writer, request_line)
            return
        log_entry(self.log_path, "connect", request_line)
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        origin_reader, origin_writer = await asyncio.open_connection(
            *request_line.split()[1].rsplit(":", 1)
        )
        await asyncio.gather(
            _pipe(reader, origin_writer),
            _pipe(origin_reader, writer),
        )

    async def _handle_absolute(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        head: bytes,
        request_line: str,
    ) -> bool:
        """Forward one absolute-form request; False when the client is done."""
        if not self._authorized(head):
            await self._reject_407(writer, request_line)
            return False
        log_entry(self.log_path, "request", request_line)
        method, url, version = request_line.split()
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)

        body = b""
        content_length = header_value(head, b"content-length")
        if content_length:
            body = await reader.readexactly(int(content_length))

        # Origins speak origin-form; the absolute-form URI is proxy-facing.
        origin_uri = parts.path or "/"
        if parts.query:
            origin_uri += f"?{parts.query}"
        origin_request_line = f"{method} {origin_uri} {version}"
        origin_head = (
            origin_request_line.encode("latin-1") + b"\r\n" + head.split(b"\r\n", 1)[1]
        )

        origin_reader, origin_writer = await asyncio.open_connection(
            parts.hostname, port
        )
        try:
            origin_writer.write(origin_head + body)
            await origin_writer.drain()
            response_head, excess = await read_head(origin_reader)
            if not response_head.strip():
                return False
            writer.write(force_connection_close(response_head))
            content_length = header_value(response_head, b"content-length")
            if content_length is not None:
                remaining = int(content_length) - len(excess)
                if remaining > 0:
                    excess += await origin_reader.readexactly(remaining)
                writer.write(excess)
            else:
                writer.write(excess)
                while chunk := await origin_reader.read(READ_CHUNK):
                    writer.write(chunk)
            await writer.drain()
        finally:
            with contextlib.suppress(OSError):
                origin_writer.close()
        return method.upper() != "HEAD"


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    """Copy src to dst until EOF, tolerating abrupt peer closes."""
    try:
        while chunk := await src.read(READ_CHUNK):
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(OSError):
            dst.close()


def main() -> None:
    """Listen on 127.0.0.1:<port>, serving CONNECT and absolute-form forwards."""
    parser = argparse.ArgumentParser()
    parser.add_argument("port", type=int)
    parser.add_argument("--require-auth")
    parser.add_argument("--log", required=True, type=Path)
    args = parser.parse_args()

    expected_b64 = None
    if args.require_auth:
        expected_b64 = base64.b64encode(args.require_auth.encode()).strip()

    proxy = ForwardProxy(args.log, expected_b64)

    async def serve() -> None:
        server = await asyncio.start_server(proxy.handle, "127.0.0.1", args.port)
        async with server:
            await server.serve_forever()

    asyncio.run(serve())


if __name__ == "__main__":
    main()
