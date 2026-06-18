"""HTTP/1.x raw socket sender."""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import time

from odda.request.response import parse_response_head
from odda.request.types import _MAX_HEADER_BYTES, _READ_CHUNK, RawResponse


async def _read_until_headers_end(
    reader: asyncio.StreamReader,
) -> bytes:
    r"""Read until ``\r\n\r\n`` and return bytes through the terminator."""
    try:
        return await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError as e:
        return e.partial
    except asyncio.LimitOverrunError:
        msg = "response headers too large"
        raise ValueError(msg) from None


async def _read_chunked_body(reader: asyncio.StreamReader) -> bytes:
    """Read a chunked transfer-encoded body and return the de-chunked bytes.

    Handles servers that close the connection mid-stream or send an empty
    chunked body gracefully by returning whatever was collected.
    """
    body = b""
    while True:
        try:
            size_line = await reader.readuntil(b"\r\n")
        except asyncio.IncompleteReadError:
            break
        size_str = size_line.strip().split(b";")[0]
        try:
            chunk_size = int(size_str, 16)
        except ValueError:
            break
        if chunk_size == 0:
            while True:
                try:
                    line = await reader.readuntil(b"\r\n")
                except asyncio.IncompleteReadError:
                    break
                if line == b"\r\n":
                    break
            break
        try:
            body += await reader.readexactly(chunk_size)
            await reader.readexactly(2)
        except (asyncio.IncompleteReadError, ConnectionError):
            break
    return body


async def send_h1(
    host: str,
    port: int,
    scheme: str,
    method: str,
    request_bytes: bytes,
    *,
    timeout: float,
    insecure: bool,
    has_body: bool,
    has_content_length: bool,
    has_transfer_encoding: bool,
) -> RawResponse:
    """Send an HTTP/1.x request over a raw socket and read the response."""
    deadline = time.monotonic() + timeout

    ssl_ctx: ssl.SSLContext | None = None
    if scheme == "https":
        ssl_ctx = ssl.create_default_context()
        if insecure:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        ssl_ctx.set_alpn_protocols(["http/1.1"])

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        msg = "timeout before connect"
        raise TimeoutError(msg)

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=ssl_ctx, limit=_MAX_HEADER_BYTES),
        timeout=remaining,
    )

    try:
        writer.write(request_bytes)
        if has_body and not has_content_length and not has_transfer_encoding:
            with contextlib.suppress(Exception, RuntimeError):
                writer.write_eof()
        await writer.drain()

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            msg = "timeout before reading response"
            raise TimeoutError(msg)

        headers_bytes = await asyncio.wait_for(
            _read_until_headers_end(reader), timeout=remaining
        )

        (
            status_code,
            content_type,
            content_length,
            transfer_encoding,
            content_encoding,
        ) = parse_response_head(headers_bytes)

        body = b""
        read_body = not (status_code in (204, 304) or method.upper() == "HEAD")
        if read_body:
            if transfer_encoding and "chunked" in transfer_encoding:
                remaining = deadline - time.monotonic()
                body = await asyncio.wait_for(
                    _read_chunked_body(reader), timeout=remaining
                )
            elif content_length is not None and content_length > 0:
                remaining = deadline - time.monotonic()
                body = await asyncio.wait_for(
                    reader.readexactly(content_length), timeout=remaining
                )
            else:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        chunk = await asyncio.wait_for(
                            reader.read(_READ_CHUNK), timeout=remaining
                        )
                    except TimeoutError:
                        break
                    if not chunk:
                        break
                    body += chunk

        return RawResponse(
            status_code=status_code,
            headers_bytes=headers_bytes,
            body_bytes=body,
            content_type=content_type,
            content_encoding=content_encoding,
        )
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
