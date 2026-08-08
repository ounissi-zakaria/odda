"""Parsing and mutation of raw HTTP request bytes."""

from __future__ import annotations

import re

from odda.request.types import ParsedRequest


def parse_request(data: bytes, line_terminator: bytes = b"\r\n") -> ParsedRequest:
    r"""Parse raw HTTP request bytes into a :class:`ParsedRequest`.

    The body is everything after the first block terminator (two line
    terminators). Header lines are split on the line terminator. Header
    values are parsed by splitting each line at the first ``:``, consuming
    one optional space after the colon, and taking the rest verbatim — no
    stripping. This is the HTTP grammar (``name ":" OWS value`` where OWS
    is one optional space), preserving trailing whitespace and internal
    bytes the agent may intend for crafting. Header names are matched
    case-insensitively for feature flags (``Content-Length``,
    ``Transfer-Encoding``, ``Host``).

    The line terminator defaults to ``\r\n`` (standard HTTP). For HTTP/2
    request files where an agent needs a literal CRLF inside a header
    value (H2→H1 downgrade smuggling), set it to ``\n`` so the parser
    splits on LF, preserving CR inside values. Only honored for HTTP/2
    request files (H1 is wire-faithful — the file goes on the socket
    verbatim, so re-framing is meaningless).

    Raises:
        ValueError: If the request line cannot be parsed.
    """
    block_terminator = line_terminator * 2
    idx = data.find(block_terminator)
    if idx == -1:
        head = data
        body = b""
    else:
        head = data[:idx]
        body = data[idx + len(block_terminator) :]

    lines = head.split(line_terminator)
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
        name = line[:colon].decode("ascii", errors="replace")
        value_start = colon + 1
        if value_start < len(line) and line[value_start : value_start + 1] == b" ":
            value_start += 1
        value = line[value_start:].decode("ascii", errors="replace")
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


def fix_content_length_bytes(data: bytes, line_terminator: bytes = b"\r\n") -> bytes:
    r"""Fix ``Content-Length`` in raw HTTP request bytes in-place.

    Replaces the value of every ``Content-Length`` header with the actual
    body length, preserving the original header name casing and the
    surrounding line terminator. If no ``Content-Length`` header exists
    and the body is non-empty, one is inserted before the blank line
    separator. All other bytes are preserved.

    The match targets only the digits of the existing value (not the
    optional trailing whitespace or the line terminator), so the
    replacement never drops a byte that terminates the header line.

    The line terminator defaults to ``\r\n`` (standard HTTP). For H2
    request files with a custom line terminator (H2->H1 downgrade
    smuggling), pass it so the block terminator (2x line terminator) and
    the inserted/rewritten lines use it correctly.

    Args:
        data: Raw HTTP request bytes.
        line_terminator: Line terminator in effect (default ``\r\n``).

    Returns:
        New bytes with ``Content-Length`` corrected.
    """
    block_terminator = line_terminator * 2
    idx = data.find(block_terminator)
    head = data[:idx] if idx != -1 else data
    body = data[idx + len(block_terminator) :] if idx != -1 else b""
    cl_value = str(len(body)).encode("ascii")

    cl_pattern = re.compile(rb"(?im)^(content-length:\s*)\d+")

    if cl_pattern.search(head):
        new_head = cl_pattern.sub(lambda _m, v=cl_value: _m.group(1) + v, head)
    elif body:
        new_head = head + line_terminator + b"Content-Length: " + cl_value
    else:
        new_head = head

    if idx == -1:
        return new_head
    return new_head + block_terminator + body
