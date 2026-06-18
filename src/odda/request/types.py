"""Shared types and constants for the request package."""

from __future__ import annotations

from dataclasses import dataclass

REQUESTS_DIR_NAME = "requests"
META_FILENAME = "meta.json"
REQUEST_FILENAME = "request"

_MAX_HEADER_BYTES = 1 << 20  # 1 MiB response header limit
_READ_CHUNK = 65536


@dataclass(frozen=True, slots=True)
class EditableMeta:
    """Sidecar metadata for an editable request (scheme/host/port)."""

    scheme: str
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class ParsedRequest:
    """Parsed view of a raw HTTP request file."""

    method: str
    path: str
    version: str
    headers: list[tuple[str, str]]
    body: bytes
    host_from_header: str | None
    has_content_length: bool
    has_transfer_encoding: bool


@dataclass(frozen=True, slots=True)
class RawResponse:
    """Raw response data before body decoding."""

    status_code: int
    headers_bytes: bytes
    body_bytes: bytes
    content_type: str | None
    content_encoding: str | None
