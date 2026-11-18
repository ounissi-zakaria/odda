from __future__ import annotations

import re

from odda.request.types import ParsedRequest


def parse_request(data: bytes, line_terminator: bytes = b"\r\n") -> ParsedRequest:
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
