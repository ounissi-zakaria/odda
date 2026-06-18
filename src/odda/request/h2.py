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
from odda.request.types import _READ_CHUNK, ParsedRequest, RawResponse


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
        msg = (
            f"Server did not negotiate h2 (negotiated: {negotiated!r}). "
            "Edit the request line to HTTP/1.1."
        )
        raise RuntimeError(msg)

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
    content_type: str | None = None
    content_encoding: str | None = None
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
                    elif hname.lower() == "content-type":
                        content_type = hvalue
                    elif hname.lower() == "content-encoding":
                        content_encoding = hvalue
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

    reason = status_reason(status_code)
    header_lines = [f"HTTP/2 {status_code} {reason}".strip()]
    for hname, hvalue in response_headers:
        if not hname.startswith(":"):
            header_lines.append(f"{hname}: {hvalue}")
    header_lines.append("")
    header_lines.append("")
    headers_bytes = "\r\n".join(header_lines).encode("ascii", errors="replace")

    return RawResponse(
        status_code=status_code,
        headers_bytes=headers_bytes,
        body_bytes=response_body,
        content_type=content_type,
        content_encoding=content_encoding,
    )
