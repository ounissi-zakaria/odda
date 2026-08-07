"""HTTP/2 raw socket sender using the ``h2`` library."""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import time
from typing import cast

import h2.config
import h2.connection
import h2.events

from odda.request.response import status_reason
from odda.request.types import (
    _READ_CHUNK,
    ConcurrentResult,
    ParsedRequest,
    RawResponse,
    StreamError,
)


async def _open_h2(
    host: str,
    port: int,
    *,
    deadline: float,
    insecure: bool,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open an HTTP/2 connection (TLS for https, ALPN h2).

    Raises ``TimeoutError`` if the deadline passes before the connection
    opens, ``RuntimeError`` if the server does not negotiate h2.
    """
    ssl_ctx = ssl.create_default_context()
    if insecure:
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
    ssl_ctx.set_alpn_protocols(["h2"])

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        msg = "timeout before connect"
        raise TimeoutError(msg)

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=ssl_ctx),
        timeout=remaining,
    )

    ssl_object = writer.transport.get_extra_info("ssl_object")
    negotiated = ssl_object.selected_alpn_protocol() if ssl_object else None
    if negotiated != "h2":
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        msg = (
            f"Server did not negotiate h2 (negotiated: {negotiated!r}). "
            "Edit the request line to HTTP/1.1."
        )
        raise RuntimeError(msg)
    return reader, writer


def _build_h2_response(
    status_code: int,
    response_headers: list[tuple[str, str]],
    body: bytes,
) -> RawResponse:
    """Reconstruct an H2 response into the H1-shaped ``RawResponse`` form.

    Extracts ``Content-Type`` and ``Content-Encoding`` from the
    pseudo-headers / headers and rebuilds an H1-shaped headers block
    (``HTTP/2 <code> <reason>`` + non-pseudo headers, CRLF-terminated)
    so the rest of odda (flow storage, body decoding) can treat H2
    responses uniformly with H1.
    """
    reason = status_reason(status_code)
    content_type: str | None = None
    content_encoding: str | None = None
    header_lines = [f"HTTP/2 {status_code} {reason}".strip()]
    for hname, hvalue in response_headers:
        if hname == ":status":
            continue
        if hname.lower() == "content-type":
            content_type = hvalue
        elif hname.lower() == "content-encoding":
            content_encoding = hvalue
        if not hname.startswith(":"):
            header_lines.append(f"{hname}: {hvalue}")
    header_lines.append("")
    header_lines.append("")
    headers_bytes = "\r\n".join(header_lines).encode("ascii", errors="replace")
    return RawResponse(
        status_code=status_code,
        headers_bytes=headers_bytes,
        body_bytes=body,
        content_type=content_type,
        content_encoding=content_encoding,
    )


async def send_h2(
    host: str,
    port: int,
    scheme: str,
    parsed: ParsedRequest,
    *,
    timeout: float,
    insecure: bool,
) -> RawResponse:
    """Send an HTTP/2 request with ALPN negotiation and h2 frame exchange."""
    deadline = time.monotonic() + timeout

    reader, writer = await _open_h2(host, port, deadline=deadline, insecure=insecure)

    config = h2.config.H2Configuration(client_side=True, header_encoding="utf-8")
    conn = h2.connection.H2Connection(config=config)
    conn.initiate_connection()

    authority = parsed.host_from_header or host
    if port not in (80, 443):
        authority = f"{authority}:{port}"

    headers: list[tuple[str, str]] = [
        (":method", parsed.method),
        (":path", parsed.path),
        (":scheme", scheme),
        (":authority", authority),
    ]
    for name, value in parsed.headers:
        if name.lower() != "host":
            headers.append((name, value))

    stream_id = conn.get_next_available_stream_id()
    conn.send_headers(stream_id, headers, end_stream=not parsed.body)
    if parsed.body:
        conn.send_data(stream_id, parsed.body, end_stream=True)

    outgoing = conn.data_to_send()
    if outgoing:
        writer.write(outgoing)
        await writer.drain()

    response_headers: list[tuple[str, str]] | None = None
    response_body = b""
    status_code = 0
    stream_ended = False

    while not stream_ended:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            msg = "timeout waiting for H2 response"
            raise TimeoutError(msg)

        try:
            data = await asyncio.wait_for(reader.read(_READ_CHUNK), timeout=remaining)
        except TimeoutError:
            msg = "timeout waiting for H2 response"
            raise TimeoutError(msg) from None

        if not data:
            break

        events = conn.receive_data(data)
        for event in events:
            if (
                isinstance(event, h2.events.ResponseReceived)
                and event.stream_id == stream_id
            ):
                # h2 returns str tuples when header_encoding="utf-8" is set,
                # but the stubs always type headers as tuple[bytes, bytes].
                response_headers = cast("list[tuple[str, str]]", event.headers)
                for hname, hvalue in response_headers:
                    if hname == ":status":
                        with contextlib.suppress(ValueError):
                            status_code = int(hvalue)
            elif (
                isinstance(event, h2.events.DataReceived)
                and event.stream_id == stream_id
            ):
                response_body += event.data
                conn.acknowledge_received_data(
                    event.flow_controlled_length, event.stream_id
                )
            elif (
                isinstance(event, h2.events.StreamEnded)
                and event.stream_id == stream_id
            ):
                stream_ended = True

        outgoing = conn.data_to_send()
        if outgoing:
            writer.write(outgoing)
            await writer.drain()

    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()

    if response_headers is None:
        msg = "No response headers received"
        raise RuntimeError(msg)

    return _build_h2_response(status_code, response_headers, response_body)


class ConcurrentConnectionError(Exception):
    """A connection-level failure aborting a concurrent H2 send.

    Carries the per-stream results gathered so far (``results``) and the
    error message. Streams that completed before the connection dropped
    have a :class:`RawResponse` in their slot; streams that did not get a
    complete response have a :class:`StreamError`. The caller (the API
    layer) writes one flow record per slot.
    """

    def __init__(self, results: list[ConcurrentResult], error_msg: str) -> None:
        """Set the partial results and the connection-level error message."""
        self.results = results
        super().__init__(error_msg)


async def send_h2_concurrent(
    host: str,
    port: int,
    scheme: str,
    parsed_requests: list[ParsedRequest],
    *,
    timeout: float,
    insecure: bool,
) -> list[ConcurrentResult]:
    """Send N requests concurrently on one HTTP/2 connection.

    Uses the last-byte single-packet technique (ADR-0020): all N
    streams' HEADERS frames go out in one TLS record so the requests
    arrive at the server near-simultaneously — the single-packet attack
    that makes race / limit-overrun reliable. For bodies >= 1 byte the
    last byte of each body is held back and flushed together as the
    final flush, so all requests complete (send end_stream) at the same
    instant.

    Per-stream errors (RST_STREAM) are isolated: that stream gets a
    :class:`StreamError`, the others continue. A connection-level error
    (GOAWAY, TLS drop) raises :class:`ConcurrentConnectionError` carrying
    the partial results (completed streams keep their responses;
    incomplete streams get a connection-error ``StreamError``).

    Args:
        host: TCP destination host.
        port: TCP destination port.
        scheme: ``http`` or ``https``.
        parsed_requests: N parsed requests (one per stream).
        timeout: Total timeout for connect + all sends + all reads.
        insecure: Skip TLS certificate verification.

    Returns:
        A list of ``ConcurrentResult`` (one per input request, in send
        order): a :class:`RawResponse` for a completed stream, or a
        :class:`StreamError` for a per-stream failure.

    Raises:
        ConcurrentConnectionError: On a connection-level failure. The
            ``results`` attribute holds the per-stream outcomes
            (responses for completed streams, errors for the rest).
        TimeoutError: On overall timeout before any response.
    """
    deadline = time.monotonic() + timeout

    reader, writer = await _open_h2(host, port, deadline=deadline, insecure=insecure)

    config = h2.config.H2Configuration(client_side=True, header_encoding="utf-8")
    conn = h2.connection.H2Connection(config=config)
    conn.initiate_connection()

    n = len(parsed_requests)
    results: list[ConcurrentResult | None] = [None] * n
    # Per-stream accumulator state, keyed by stream_id.
    stream_headers: dict[int, list[tuple[str, str]]] = {}
    stream_bodies: dict[int, bytearray] = {}
    stream_done: set[int] = set()
    stream_index: dict[int, int] = {}  # stream_id -> send-order index

    # Phase 1: send all HEADERS frames (no drain between -> one TLS
    # record carries them all). Empty-body streams end here; body streams
    # stay open for the last-byte phase.
    has_body_streams: list[tuple[int, bytes]] = []
    for i, parsed in enumerate(parsed_requests):
        authority = parsed.host_from_header or host
        if port not in (80, 443):
            authority = f"{authority}:{port}"
        headers: list[tuple[str, str]] = [
            (":method", parsed.method),
            (":path", parsed.path),
            (":scheme", scheme),
            (":authority", authority),
        ]
        for name, value in parsed.headers:
            if name.lower() != "host":
                headers.append((name, value))

        stream_id = conn.get_next_available_stream_id()
        stream_index[stream_id] = i
        end_stream = not bool(parsed.body)
        conn.send_headers(stream_id, headers, end_stream=end_stream)
        if parsed.body:
            has_body_streams.append((stream_id, parsed.body))

    # Flush the HEADERS (and the connection preface) as one write.
    outgoing = conn.data_to_send()
    if outgoing:
        writer.write(outgoing)
        await writer.drain()

    # Phase 2 (only if any body streams): last-byte single-packet. Send
    # all-but-last byte of each body with end_stream=False (no drain
    # between), then send the last byte of ALL bodies with end_stream=True
    # in one final flush. All requests complete at the same instant.
    if has_body_streams:
        for stream_id, body in has_body_streams:
            if len(body) > 1:
                conn.send_data(stream_id, body[:-1], end_stream=False)
        outgoing = conn.data_to_send()
        if outgoing:
            writer.write(outgoing)
            await writer.drain()
        for stream_id, body in has_body_streams:
            last = body[-1:]
            conn.send_data(stream_id, last, end_stream=True)
        outgoing = conn.data_to_send()
        if outgoing:
            writer.write(outgoing)
            await writer.drain()

    # Phase 3: read all responses. One read loop dispatching events to
    # per-stream accumulators. Per-stream RST is isolated; connection
    # termination / EOF / timeout is a connection-level failure (raises
    # ConcurrentConnectionError carrying the partial results, ADR-0020).
    conn_error: str | None = None
    try:
        while len(stream_done) < n and conn_error is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                conn_error = "timeout waiting for H2 response"
                break

            try:
                data = await asyncio.wait_for(
                    reader.read(_READ_CHUNK), timeout=remaining
                )
            except TimeoutError:
                conn_error = "timeout waiting for H2 response"
                break

            if not data:
                # Clean EOF before all streams done: connection-level
                # failure.
                conn_error = "connection closed before response complete"
                break

            events = conn.receive_data(data)
            for event in events:
                if isinstance(event, h2.events.ResponseReceived):
                    sid = event.stream_id
                    if sid in stream_index:
                        stream_headers[sid] = cast(
                            "list[tuple[str, str]]", event.headers
                        )
                elif isinstance(event, h2.events.DataReceived):
                    sid = event.stream_id
                    if sid in stream_index:
                        stream_bodies.setdefault(sid, bytearray()).extend(event.data)
                        conn.acknowledge_received_data(
                            event.flow_controlled_length, sid
                        )
                elif isinstance(event, h2.events.StreamEnded):
                    sid = event.stream_id
                    if sid in stream_index and sid not in stream_done:
                        idx = stream_index[sid]
                        headers = stream_headers.get(sid, [])
                        status_code = 0
                        for hname, hvalue in headers:
                            if hname == ":status":
                                with contextlib.suppress(ValueError):
                                    status_code = int(hvalue)
                        results[idx] = _build_h2_response(
                            status_code,
                            headers,
                            bytes(stream_bodies.get(sid, b"")),
                        )
                        stream_done.add(sid)
                elif isinstance(event, h2.events.StreamReset):
                    # Per-stream error: isolated. Other streams continue.
                    sid = event.stream_id
                    if sid in stream_index and sid not in stream_done:
                        idx = stream_index[sid]
                        results[idx] = StreamError(
                            error=f"stream reset (error code {event.error_code})"
                        )
                        stream_done.add(sid)
                elif isinstance(event, h2.events.ConnectionTerminated):
                    # Connection-level: abort all incomplete streams.
                    conn_error = (
                        f"connection terminated by server "
                        f"(error code {event.error_code})"
                    )
                    break

            outgoing = conn.data_to_send()
            if outgoing:
                writer.write(outgoing)
                await writer.drain()
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    # Fill any remaining None slots (per-stream isolation left them, or a
    # connection error aborted them).
    final: list[ConcurrentResult] = []
    for r in results:
        if r is None:
            final.append(StreamError(error=conn_error or "no response received"))
        else:
            final.append(r)

    if conn_error is not None:
        raise ConcurrentConnectionError(final, conn_error)
    return final
