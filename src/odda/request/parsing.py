"""Parsing and mutation of raw HTTP request bytes."""

from __future__ import annotations

import re

from odda.request.types import ParsedRequest


def parse_request(data: bytes) -> ParsedRequest:
    r"""Parse raw HTTP request bytes into a :class:`ParsedRequest`.

    The body is everything after the first ``\r\n\r\n`` separator.
    Header names are matched case-insensitively for feature flags
    (``Content-Length``, ``Transfer-Encoding``, ``Host``).

    Raises:
        ValueError: If the request line cannot be parsed.
    """
    sep = b"\r\n\r\n"
    idx = data.find(sep)
    if idx == -1:
        head = data
        body = b""
    else:
        head = data[:idx]
        body = data[idx + len(sep) :]

    lines = head.split(b"\r\n")
    if not lines or not lines[0]:
        msg = "Empty request — no request line"
        raise ValueError(msg)

    request_line = lines[0].decode("ascii", errors="replace")
    parts = request_line.split(" ", 2)
    if len(parts) < 3:
        msg = f"Invalid request line: {request_line!r}"
        raise ValueError(msg)

    method, path, version = parts

    headers: list[tuple[str, str]] = []
    host_from_header: str | None = None
    has_content_length = False
    has_transfer_encoding = False

    for line in lines[1:]:
        if not line:
            continue
        colon = line.find(b":")
        if colon == -1:
            continue
        name = line[:colon].decode("ascii", errors="replace").strip()
        value = line[colon + 1 :].decode("ascii", errors="replace").strip()
        headers.append((name, value))

        lower = name.lower()
        if lower == "host" and host_from_header is None:
            host_from_header = value
        elif lower == "content-length":
            has_content_length = True
        elif lower == "transfer-encoding":
            has_transfer_encoding = True

    return ParsedRequest(
        method=method,
        path=path,
        version=version,
        headers=headers,
        body=body,
        host_from_header=host_from_header,
        has_content_length=has_content_length,
        has_transfer_encoding=has_transfer_encoding,
    )


def fix_content_length_bytes(data: bytes) -> bytes:
    r"""Fix ``Content-Length`` in raw HTTP request bytes in-place.

    Replaces the value of every ``Content-Length`` header with the actual
    body length, preserving the original header name casing. If no
    ``Content-Length`` header exists and the body is non-empty, one is
    inserted before the blank line separator. All other bytes are preserved.

    Args:
        data: Raw HTTP request bytes.

    Returns:
        New bytes with ``Content-Length`` corrected.
    """
    sep = b"\r\n\r\n"
    idx = data.find(sep)
    head = data[:idx] if idx != -1 else data
    body = data[idx + len(sep) :] if idx != -1 else b""
    cl_value = str(len(body)).encode("ascii")

    cl_pattern = re.compile(rb"(?im)^(content-length:)\s*\d+\s*$")

    if cl_pattern.search(head):
        new_head = cl_pattern.sub(rb"\1 " + cl_value, head)
    elif body:
        new_head = head + b"\r\nContent-Length: " + cl_value
    else:
        new_head = head

    if idx == -1:
        return new_head
    return new_head + b"\r\n\r\n" + body
