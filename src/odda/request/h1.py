from __future__ import annotations

import asyncio
import contextlib
import ssl
import time

from odda.request.response import parse_response_head
from odda.request.types import (
    _MAX_HEADER_BYTES,
    _READ_CHUNK,
    ConcurrentResult,
    ParsedRequest,
    RawResponse,
    StreamError,
)


class PipelineError(Exception):
    def __init__(
        self,
        failed_step: int,
        partial_responses: list[RawResponse],
        cause: Exception,
    ) -> None:
        self.failed_step = failed_step
        self.partial_responses = partial_responses
        self.cause = cause
        super().__init__(str(cause) or type(cause).__name__)


async def _read_until_headers_end(
    reader: asyncio.StreamReader,
) -> bytes:
    try:
        return await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError as e:
        if e.partial:
            msg = (
                "connection closed before response headers ended "
                f"(got {len(e.partial)} partial bytes)"
            )
        else:
            msg = "connection closed before response headers"
        raise ConnectionError(msg) from None
    except OSError as e:
        raise ConnectionError("connection closed before response headers") from e
    except asyncio.LimitOverrunError:
        msg = "response headers too large"
        raise ValueError(msg) from None


async def _read_chunked_body(reader: asyncio.StreamReader) -> bytes:
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


async def _read_one_response(
    reader: asyncio.StreamReader,
    method: str,
    *,
    deadline: float,
) -> RawResponse:
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
            body = await asyncio.wait_for(_read_chunked_body(reader), timeout=remaining)
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


def _make_ssl_ctx(scheme: str, *, insecure: bool) -> ssl.SSLContext | None:
    if scheme != "https":
        return None
    ssl_ctx = ssl.create_default_context()
    if insecure:
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
    ssl_ctx.set_alpn_protocols(["http/1.1"])
    return ssl_ctx


async def _open_h1(
    host: str,
    port: int,
    scheme: str,
    *,
    deadline: float,
    insecure: bool,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    ssl_ctx = _make_ssl_ctx(scheme, insecure=insecure)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        msg = "timeout before connect"
        raise TimeoutError(msg)
    return await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=ssl_ctx, limit=_MAX_HEADER_BYTES),
        timeout=remaining,
    )


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
    deadline = time.monotonic() + timeout

    reader, writer = await _open_h1(
        host, port, scheme, deadline=deadline, insecure=insecure
    )

    try:
        writer.write(request_bytes)
        if has_body and not has_content_length and not has_transfer_encoding:
            with contextlib.suppress(Exception, RuntimeError):
                writer.write_eof()
        await writer.drain()

        return await _read_one_response(reader, method, deadline=deadline)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def send_h1_pipeline(
    host: str,
    port: int,
    scheme: str,
    requests: list[tuple[bytes, ParsedRequest]],
    *,
    timeout: float,
    insecure: bool,
    pipelining: bool = False,
) -> list[RawResponse]:
    # See ADR-0019.
    for _request_bytes, parsed in requests:
        if (
            bool(parsed.body)
            and not parsed.has_content_length
            and not parsed.has_transfer_encoding
        ):
            msg = (
                "request has a body with no Content-Length and no "
                "Transfer-Encoding, which requires half-closing the socket "
                "and is incompatible with multi-name send"
            )
            raise ValueError(msg)

    deadline = time.monotonic() + timeout
    reader, writer = await _open_h1(
        host, port, scheme, deadline=deadline, insecure=insecure
    )

    try:
        if pipelining:
            for request_bytes, _parsed in requests:
                writer.write(request_bytes)
            with contextlib.suppress(OSError):
                await writer.drain()
            responses: list[RawResponse] = []
            for i, (_request_bytes, parsed) in enumerate(requests):
                try:
                    responses.append(
                        await _read_one_response(
                            reader, parsed.method, deadline=deadline
                        )
                    )
                except Exception as exc:
                    raise PipelineError(i, responses, exc) from exc
            return responses
        responses = []
        for i, (request_bytes, parsed) in enumerate(requests):
            try:
                writer.write(request_bytes)
                await writer.drain()
                responses.append(
                    await _read_one_response(reader, parsed.method, deadline=deadline)
                )
            except Exception as exc:
                raise PipelineError(i, responses, exc) from exc
        return responses
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _send_one_h1_parallel(
    index: int,
    request_bytes: bytes,
    parsed: ParsedRequest,
    host: str,
    port: int,
    scheme: str,
    *,
    deadline: float,
    insecure: bool,
) -> tuple[int, ConcurrentResult]:
    try:
        reader, writer = await _open_h1(
            host, port, scheme, deadline=deadline, insecure=insecure
        )
    except Exception as exc:
        msg = str(exc) or type(exc).__name__
        return index, StreamError(error=f"connect failed: {msg}")
    try:
        writer.write(request_bytes)
        if (
            bool(parsed.body)
            and not parsed.has_content_length
            and not parsed.has_transfer_encoding
        ):
            with contextlib.suppress(Exception, RuntimeError):
                writer.write_eof()
        await writer.drain()
        raw = await _read_one_response(reader, parsed.method, deadline=deadline)
    except Exception as exc:
        msg = str(exc) or type(exc).__name__
        return index, StreamError(error=msg)
    else:
        return index, raw
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def send_h1_parallel(
    host: str,
    port: int,
    scheme: str,
    requests: list[tuple[bytes, ParsedRequest]],
    *,
    timeout: float,
    insecure: bool,
) -> list[ConcurrentResult]:
    # See ADR-0020.
    deadline = time.monotonic() + timeout
    tasks = [
        _send_one_h1_parallel(
            i,
            request_bytes,
            parsed,
            host,
            port,
            scheme,
            deadline=deadline,
            insecure=insecure,
        )
        for i, (request_bytes, parsed) in enumerate(requests)
    ]
    gathered = await asyncio.gather(*tasks)
    by_index: dict[int, ConcurrentResult] = dict(gathered)
    return [by_index[i] for i in range(len(requests))]
