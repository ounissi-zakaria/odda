"""Parsing and mutation of raw HTTP request bytes."""

from __future__ import annotations

from dataclasses import replace

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


def fix_content_length(parsed: ParsedRequest) -> ParsedRequest:
    """Return a new :class:`ParsedRequest` with ``Content-Length`` corrected.

    Replaces every ``Content-Length`` header value with ``len(body)``. If no
    ``Content-Length`` header exists and the body is non-empty, one is
    appended. Sets ``has_content_length`` to ``True`` when a body exists.
    """
    cl_value = str(len(parsed.body))
    new_headers: list[tuple[str, str]] = []
    found_cl = False
    for name, value in parsed.headers:
        if name.lower() == "content-length":
            new_headers.append((name, cl_value))
            found_cl = True
        else:
            new_headers.append((name, value))
    if not found_cl and parsed.body:
        new_headers.append(("Content-Length", cl_value))

    return replace(
        parsed,
        headers=new_headers,
        has_content_length=found_cl or bool(parsed.body),
    )


def build_request_bytes(parsed: ParsedRequest) -> bytes:
    """Reconstruct raw HTTP request bytes from a :class:`ParsedRequest`."""
    lines = [f"{parsed.method} {parsed.path} {parsed.version}".encode("ascii")]
    for name, value in parsed.headers:
        lines.append(f"{name}: {value}".encode("ascii", errors="replace"))
    lines.append(b"")
    lines.append(b"")
    return b"\r\n".join(lines) + parsed.body
