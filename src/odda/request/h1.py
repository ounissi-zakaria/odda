"""HTTP/1.x raw socket sender."""

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
    """A mid-sequence failure in :func:`send_h1_pipeline`.

    Carries the responses that were read *before* the failure
    (``partial_responses``) and the index of the step that failed
    (``failed_step``, 0-based), so the caller can write response flows
    for steps 0..failed_step-1, an error flow for ``failed_step``, and
    aborted flows for the rest. The connection is already closed when
    this is raised.
    """

    def __init__(
        self,
        failed_step: int,
        partial_responses: list[RawResponse],
        cause: Exception,
    ) -> None:
        """Set the failing step index, partial responses, and cause."""
        self.failed_step = failed_step
        self.partial_responses = partial_responses
        self.cause = cause
        super().__init__(str(cause) or type(cause).__name__)


async def _read_until_headers_end(
    reader: asyncio.StreamReader,
) -> bytes:
    r"""Read until ``\r\n\r\n`` and return bytes through the terminator.

    Raises ``ConnectionError`` if EOF is reached before the header
    terminator — a clean (or abrupt) close with no (or partial) response
    headers. Swallowing that case and returning the empty/partial bytes
    produces a ``status_code: 0`` pseudo-response that looks like a
    successful empty response, masking a dropped connection. The
    pipelining loop and single-shot caller both translate the raise into
    a descriptive error flow ("connection closed before response
    headers") instead of an ambiguous ``status_code: 0, error: null``.
    """
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


async def _read_one_response(
    reader: asyncio.StreamReader,
    method: str,
    *,
    deadline: float,
) -> RawResponse:
    """Read one HTTP/1.x response from ``reader`` before ``deadline``.

    Shared by :func:`send_h1` (single-shot) and :func:`send_h1_pipeline`
    (multi-request). Reads headers, then the body (chunked, content-length,
    or until connection close), and returns a :class:`RawResponse`.
    """
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
    """Build an SSL context for an H1/H2 connection, ALPN http/1.1."""
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
    """Open an HTTP/1.x connection (TLS for https, ALPN http/1.1)."""
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
    """Send an HTTP/1.x request over a raw socket and read the response."""
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
    """Send multiple HTTP/1.x requests on one connection, read all responses.

    One TCP/TLS connection (ALPN http/1.1). All requests are written with
    their exact bytes (no re-encoding, no Content-Length fixing); the
    connection stays open across sends and is closed at the end.

    ``pipelining=False`` (default, sequential keep-alive): for each request,
    write its bytes, drain, then read one response before writing the next.
    This covers response-queue poisoning (send smuggling → read resp1 →
    send victim → read resp2) and same-connection CL.0 confirmation.

    ``pipelining=True`` (true H1 pipelining): write every request's bytes
    up front, drain once, then read every response in order. This covers
    victim-consumption, where the victim request must arrive while the
    server is still parsing the smuggling POST's body.

    Bare-body requests (a body with no Content-Length and no
    Transfer-Encoding) are rejected before the socket opens: they require
    ``write_eof`` to terminate the body, which half-closes the connection
    and makes subsequent sends impossible. Every documented smuggling
    variant uses at least one framing header, so nothing legitimate is
    blocked.

    Raises:
        ValueError: If any request has a body with no Content-Length and no
            Transfer-Encoding (the bare-body shape that would require
            half-closing the socket).
        PipelineError: If a read or write fails mid-sequence. Carries the
            responses read before the failure and the failing step index;
            the caller writes response flows for steps before the failure,
            an error flow for the failing step, and aborted flows for the
            rest (ADR-0019).
    """
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
            # Tolerate a close during the write-all phase: the server is
            # allowed to read request 1, send its response, and close
            # before we finish writing the pipelined follow-ups. If the
            # transport closes mid-drain, suppress the write error and
            # let the read loop below detect the real EOF — that yields
            # the correct per-step attribution (step 1's buffered
            # response is read, step 2 hits EOF and becomes the failed
            # step, steps 3..N become aborted). Without this, a fast
            # close aborts everything as "step 1 failed" and loses step
            # 1's response. ``OSError`` covers every connection-close
            # variant asyncio raises here (``ConnectionResetError``,
            # ``BrokenPipeError``, ``ssl.SSLError``); the read loop is
            # the authoritative attribution path, so non-transport errors
            # are left to propagate.
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
    """Open one H1 connection, send one request, return (index, result)."""
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
    """Send N identical requests on N parallel HTTP/1.1 connections.

    H1 cannot stream-multiplex on one connection, and H1 pipelining is
    server-side sequential (only the first request lands in a race
    window). So for concurrent delivery over H1 we open N parallel
    TCP/TLS connections in the same process and fire one request on
    each. Tighter than bash-parallel (no ~100-300ms process startup
    spread) but each connection does its own TLS handshake, so there's
    residual handshake spread. Matches Burp's "send group in parallel"
    (ADR-0020).

    Per-connection failures are isolated: that slot gets a
    :class:`StreamError`, the others continue. There is no
    connection-level abort (each request is on its own connection).

    Args:
        host: TCP destination host.
        port: TCP destination port.
        scheme: ``http`` or ``https``.
        requests: N (request_bytes, parsed) tuples.
        timeout: Total timeout for connect + send + read, per connection.
        insecure: Skip TLS certificate verification.

    Returns:
        A list of ``ConcurrentResult`` (one per input request, in send
        order).
    """
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
