"""Response header parsing and body decoding."""

from __future__ import annotations

import contextlib
import gzip
import zlib
from http import HTTPStatus

import brotli
import zstandard


def parse_response_head(
    data: bytes,
) -> tuple[int, str | None, int | None, str | None, str | None]:
    r"""Parse response headers bytes (through the terminal ``\r\n\r\n``).

    Returns:
        (status_code, content_type, content_length, transfer_encoding,
        content_encoding)
    """
    text = data.decode("ascii", errors="replace")
    lines = text.split("\r\n")
    status_line = lines[0]
    status_code = 0
    parts = status_line.split(" ", 2)
    if len(parts) >= 2:
        with contextlib.suppress(ValueError):
            status_code = int(parts[1])

    content_type: str | None = None
    content_length: int | None = None
    transfer_encoding: str | None = None
    content_encoding: str | None = None

    for line in lines[1:]:
        if not line:
            continue
        colon = line.find(":")
        if colon == -1:
            continue
        name = line[:colon].strip().lower()
        value = line[colon + 1 :].strip()
        if name == "content-type":
            content_type = value
        elif name == "content-length":
            with contextlib.suppress(ValueError):
                content_length = int(value)
        elif name == "transfer-encoding":
            transfer_encoding = value.lower()
        elif name == "content-encoding":
            content_encoding = value.lower()

    return (
        status_code,
        content_type,
        content_length,
        transfer_encoding,
        content_encoding,
    )


def decode_content_encoding(data: bytes, encoding: str | None) -> bytes:
    """Decode ``Content-Encoding`` (gzip/deflate/br/zstd).

    If the declared encoding doesn't match the actual bytes (e.g. a server
    sends ``Content-Encoding: gzip`` but returns plain text), the original
    bytes are returned unchanged.
    """
    if not encoding or not data:
        return data
    try:
        if encoding == "gzip":
            return gzip.decompress(data)
        if encoding == "deflate":
            try:
                return zlib.decompress(data)
            except zlib.error:
                return zlib.decompress(data, -zlib.MAX_WBITS)
        if encoding == "br":
            return brotli.decompress(data)
        if encoding == "zstd":
            return zstandard.ZstdDecompressor().decompress(data)
    except Exception:
        return data
    return data


def status_reason(code: int) -> str:
    """Return the standard HTTP reason phrase for a status code."""
    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return ""
