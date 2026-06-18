"""File-based storage for mitmproxy flows.

Captured flows are reconstructed into HTTP/1.x-shaped text files. mitmproxy
normalizes H2/H3 traffic into its H1-shaped object model before the addon
sees it, so files for H2/H3 traffic carry the original version string but
are readable rather than wire-replayable.
"""

from __future__ import annotations

import contextlib
import itertools
import json
from pathlib import Path
from typing import Any

# Content types to exclude from response body storage
EXCLUDED_CONTENT_TYPES: tuple[str, ...] = (
    "font/",
    "video/",
    "audio/",
    "image/",
)

# Map MIME types to file extensions
_MIME_TO_EXT: dict[str, str] = {
    "application/json": ".json",
    "application/javascript": ".js",
    "text/javascript": ".js",
    "application/x-javascript": ".js",
    "text/html": ".html",
    "application/xhtml+xml": ".xhtml",
    "text/css": ".css",
    "text/xml": ".xml",
    "application/xml": ".xml",
    "application/atom+xml": ".xml",
    "application/rss+xml": ".xml",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/tab-separated-values": ".tsv",
    "application/x-yaml": ".yaml",
    "text/yaml": ".yaml",
    "application/yaml": ".yaml",
    "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/gzip": ".gz",
    "application/x-gzip": ".gz",
    "application/x-tar": ".tar",
    "application/octet-stream": ".bin",
    "application/x-protobuf": ".pb",
    "application/grpc": ".grpc",
    "application/grpc+proto": ".grpc",
    "application/grpc+json": ".grpc",
    "application/x-www-form-urlencoded": ".txt",
    "multipart/form-data": ".txt",
    "application/msgpack": ".msgpack",
    "application/x-msgpack": ".msgpack",
    "application/bson": ".bson",
    "application/wasm": ".wasm",
    "application/x-font-ttf": ".ttf",
    "application/x-font-otf": ".otf",
    "application/font-woff": ".woff",
    "application/font-woff2": ".woff2",
    "font/ttf": ".ttf",
    "font/otf": ".otf",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "text/markdown": ".md",
    "text/calendar": ".ics",
    "text/vcard": ".vcf",
    "application/ld+json": ".jsonld",
    "application/schema+json": ".json",
    "application/manifest+json": ".json",
    "application/x-ndjson": ".ndjson",
    "application/vnd.api+json": ".json",
}

DATA_DIR: Path = Path.cwd() / ".odda"

_READ_ONLY = 0o444


def set_data_dir(path: Path | str) -> None:
    """Set the directory used for flow storage.

    Args:
        path: Directory path. Created if it does not exist.
    """
    global DATA_DIR
    DATA_DIR = Path(path)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _flows_dir() -> Path:
    """Get the directory holding flows.jsonl and per-flow directories.

    Returns:
        Path to ``<DATA_DIR>/flows/``, created if missing.
    """
    flows_dir = DATA_DIR / "flows"
    flows_dir.mkdir(parents=True, exist_ok=True)
    return flows_dir


def should_store_body(content_type: str | None) -> bool:
    """Check if body should be stored based on content type.

    Args:
        content_type: MIME type of the content.

    Returns:
        True if body should be stored, False otherwise.
    """
    return not content_type or not content_type.startswith(EXCLUDED_CONTENT_TYPES)


def _mime_to_ext(content_type: str | None) -> str:
    """Map a MIME type to a file extension.

    Args:
        content_type: MIME type string (e.g. ``application/json``).

    Returns:
        File extension including leading dot, or ``.bin`` for unknown types.
    """
    if not content_type:
        return ".bin"
    mime = content_type.split(";")[0].strip().lower()
    return _MIME_TO_EXT.get(mime, ".bin")


def _build_request_bytes(flow) -> bytes:
    """Reconstruct a raw HTTP request from a mitmproxy flow.

    Args:
        flow: mitmproxy flow object.

    Returns:
        Bytes forming a complete HTTP request: request line, headers, blank
        line, then body. Uses CRLF line endings. Body is the decoded content
        from mitmproxy (``flow.request.content``). The HTTP version is taken
        from ``flow.request.http_version``.
    """
    request = flow.request
    lines = [f"{request.method} {request.path} {request.http_version}".encode("ascii")]
    for name, value in request.headers.items():
        lines.append(f"{name}: {value}".encode("ascii", errors="replace"))
    lines.append(b"")
    lines.append(b"")
    head = b"\r\n".join(lines)
    body = request.content or b""
    return head + body


def _build_response_headers_bytes(flow) -> bytes:
    """Reconstruct raw HTTP response headers from a mitmproxy flow.

    Args:
        flow: mitmproxy flow object with a response.

    Returns:
        Bytes forming the status line, headers, and a trailing blank line,
        using CRLF line endings. No body is included. The HTTP version is
        taken from ``flow.response.http_version``.
    """
    response = flow.response
    lines = [
        f"{response.http_version} {response.status_code} {response.reason}".encode(
            "ascii", errors="replace"
        )
    ]
    for name, value in response.headers.items():
        lines.append(f"{name}: {value}".encode("ascii", errors="replace"))
    lines.append(b"")
    lines.append(b"")
    return b"\r\n".join(lines)


def _write_readonly(path: Path, data: bytes) -> None:
    """Write bytes to a file then make it read-only.

    Args:
        path: Destination file path.
        data: Bytes to write.
    """
    path.write_bytes(data)
    with contextlib.suppress(OSError):
        path.chmod(_READ_ONLY)


def _scan_max_flow_id(flows_dir: Path) -> int:
    """Find the highest zero-padded flow id present on disk.

    Args:
        flows_dir: Directory containing per-flow subdirectories.

    Returns:
        The max numeric id found, or 0 if none exist.
    """
    max_id = 0
    for entry in flows_dir.iterdir():
        if not entry.is_dir():
            continue
        try:
            max_id = max(max_id, int(entry.name))
        except ValueError:
            continue
    return max_id


class FlowFileAddon:
    """Mitmproxy addon that stores flows as read-only files on disk."""

    def __init__(self) -> None:
        """Initialize the addon and seed the flow id counter from disk."""
        self._flows_dir = _flows_dir()
        self._counter = itertools.count(_scan_max_flow_id(self._flows_dir) + 1)
        self._pending_flows: dict[int, str] = {}
        self._jsonl_path = self._flows_dir / "flows.jsonl"

    def _alloc_flow_id(self) -> str:
        """Allocate a unique zero-padded flow id by atomically creating its dir.

        Uses ``mkdir`` as the uniqueness primitive so multiple processes can
        allocate concurrently without a lock: the filesystem picks one winner
        per id, and losers bump the counter and retry. Ids are unique but not
        contiguous under contention (acceptable — sortability matters, not
        density).

        Returns:
            A zero-padded flow id string matching the created directory name.
        """
        while True:
            flow_id = f"{next(self._counter):05d}"
            try:
                (self._flows_dir / flow_id).mkdir()
            except FileExistsError:
                continue
            return flow_id

    def _append_jsonl(self, record: dict[str, Any]) -> None:
        """Append one record to flows.jsonl.

        Opens in append mode (``O_APPEND``) so the single line write is
        atomic under concurrent appenders. The file keeps default
        permissions so appends keep working.

        Args:
            record: JSON-serializable flow summary.
        """
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def request(self, flow) -> None:
        """Handle request - create flow dir and write the request file.

        The flow dir and request file are durable immediately, even if no
        response ever arrives. The flows.jsonl line is appended only when
        the flow completes (response or error).

        Args:
            flow: mitmproxy flow object.
        """
        flow_id = self._alloc_flow_id()
        flow_dir = self._flows_dir / flow_id
        _write_readonly(flow_dir / "request", _build_request_bytes(flow))
        self._pending_flows[flow.id] = flow_id

    def response(self, flow) -> None:
        """Handle response - write response files and append the jsonl line.

        Args:
            flow: mitmproxy flow object.
        """
        flow_id = self._pending_flows.pop(flow.id, None)
        if flow_id is None:
            return

        flow_dir = self._flows_dir / flow_id
        response = flow.response
        request = flow.request

        content_type = response.headers.get("Content-Type", "")

        body_file: str | None = None
        if should_store_body(content_type) and response.content:
            ext = _mime_to_ext(content_type)
            body_name = f"response_body{ext}"
            _write_readonly(flow_dir / body_name, response.content)
            body_file = f"flows/{flow_id}/{body_name}"

        _write_readonly(
            flow_dir / "response_headers", _build_response_headers_bytes(flow)
        )

        total_duration_ms: float | None = None
        if response.timestamp_end and request.timestamp_start:
            elapsed = response.timestamp_end - request.timestamp_start
            total_duration_ms = elapsed * 1000

        self._append_jsonl(
            {
                "id": flow_id,
                "method": request.method,
                "host": request.host,
                "path": request.path,
                "status_code": response.status_code,
                "total_duration_ms": total_duration_ms,
                "body_file": body_file,
                "error": None,
            }
        )

    def error(self, flow) -> None:
        """Handle flow errors - write an error file and append the jsonl line.

        Args:
            flow: mitmproxy flow object with ``flow.error`` populated.
        """
        flow_id = self._pending_flows.pop(flow.id, None)
        if flow_id is None:
            return

        flow_dir = self._flows_dir / flow_id
        msg = flow.error.msg if flow.error else "unknown error"
        _write_readonly(flow_dir / "error", msg.encode("utf-8"))

        self._append_jsonl(
            {
                "id": flow_id,
                "method": flow.request.method,
                "host": flow.request.host,
                "path": flow.request.path,
                "status_code": None,
                "total_duration_ms": None,
                "body_file": None,
                "error": msg,
            }
        )
