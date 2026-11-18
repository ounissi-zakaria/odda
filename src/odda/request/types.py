from __future__ import annotations

from dataclasses import dataclass

REQUESTS_DIR_NAME = "requests"
META_FILENAME = "meta.json"
REQUEST_FILENAME = "request"

_MAX_HEADER_BYTES = 1 << 20
_READ_CHUNK = 65536


@dataclass(frozen=True, slots=True)
class EditableMeta:
    scheme: str
    host: str
    port: int
    line_terminator: bytes = b"\r\n"


@dataclass(frozen=True, slots=True)
class ParsedRequest:
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
    status_code: int
    headers_bytes: bytes
    body_bytes: bytes
    content_type: str | None
    content_encoding: str | None


@dataclass(frozen=True, slots=True)
class StreamError:
    error: str


ConcurrentResult = RawResponse | StreamError
