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
from dataclasses import dataclass
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
    "image/jpeg": ".jpeg",
    "image/jpg": ".jpeg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/apng": ".apng",
    "image/avif": ".avif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/ogg": ".ogv",
    "video/x-msvideo": ".avi",
    "video/quicktime": ".mov",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/webm": ".weba",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/x-midi": ".mid",
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

# flow.metadata key the browser-auth addon (proxy.py) stamps with the parsed
# proxy credentials: (username, password). The username is the browser's
# browser_id token (ADR-0031).
PROXYAUTH_METADATA_KEY = "proxyauth"

# flow.metadata key marking flows the auth addon synthesized itself (the
# canary seeding challenge and its acknowledgment). FlowFileAddon skips
# tagged flows entirely: no flow id, no directory, no jsonl line.
AUTH_CHALLENGE_TAG = "odda_auth_challenge"


def set_data_dir(path: Path | str) -> None:
    """Set the directory used for flow storage.

    The directory is *remembered* but not created; it (and its parents)
    appear lazily on the first state write (flows, requests, browsers,
    userscripts), each of which mkdirs its own target path. This keeps
    the data dir absent until odda is actually used.

    Args:
        path: Directory path.
    """
    global DATA_DIR, _writer
    DATA_DIR = Path(path)
    # The writer snapshots the flows dir at creation; a new data dir
    # invalidates it (the MCP surface can host many sessions in one
    # process — each lifespan re-points the data dir). It re-creates on
    # next use, re-seeding the flow id counter from the new location.
    _writer = None


def _flows_dir() -> Path:
    """Path to the directory holding flows.jsonl and per-flow directories.

    Returns the path without creating it; the directory (and its parents)
    appears lazily on the first flow write.
    """
    return DATA_DIR / "flows"


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

    Iterates ``headers.items(multi=True)`` so each duplicate header is emitted
    on its own line, rather than the folding ``headers.items()`` which joins
    same-name headers with ``", "`` (RFC 7230 §3.2.2). The captured file
    faithfully records what the client sent: two ``cookie`` fragments (from a
    non-conformant H1 UA, a hand-built request, or an HTTP/2 client splitting
    cookie pairs per RFC 7540 §8.1.2.5) appear as two ``cookie:`` lines, not a
    joined or ``, "``-folded form. Splitting duplicates is always legal in
    HTTP/1.1 and is the safer default than folding, which is wrong for headers
    whose values contain commas (see :func:`_build_response_headers_bytes` for
    ``Set-Cookie``). See ADR-0013 and REQUEST.md ("the ``request`` file is
    origin-form and goes on the wire verbatim").
    """
    request = flow.request
    lines = [f"{request.method} {request.path} {request.http_version}".encode("ascii")]
    for name, value in request.headers.items(multi=True):
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

    Iterates ``headers.items(multi=True)`` so each duplicate header is emitted
    on its own line, rather than the folding ``headers.items()`` which joins
    same-name headers with ``", "`` (RFC 7230 §3.2.2). This is required for
    ``Set-Cookie``: RFC 6265 §5.3 forbids folding because attribute values
    (e.g. ``Expires=Wed, 09 Jun 2021 ...``) contain commas, so a folded
    ``set-cookie: a=1, b=2`` is unparseable. Splitting duplicates is also the
    safer default for any other duplicated response header whose values may
    contain commas. See :func:`_build_request_bytes` — both sides now use
    ``items(multi=True)`` uniformly (ADR-0013).
    """
    response = flow.response
    lines = [
        f"{response.http_version} {response.status_code} {response.reason}".encode(
            "ascii", errors="replace"
        )
    ]
    for name, value in response.headers.items(multi=True):
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
        The max numeric id found, or 0 if the directory is absent or empty.
    """
    max_id = 0
    if not flows_dir.exists():
        return max_id
    for entry in flows_dir.iterdir():
        if not entry.is_dir():
            continue
        try:
            max_id = max(max_id, int(entry.name))
        except ValueError:
            continue
    return max_id


class FlowRecordWriter:
    """Shared flow-record writer for mitmproxy addons and the request sender.

    Owns a single monotonic counter so concurrent flow allocation (mitmproxy
    capture vs ``request_send``) avoids racing on already-claimed ids.
    Uniqueness is still guaranteed by ``mkdir``; the counter only minimizes
    retries.
    """

    def __init__(self) -> None:
        """Initialize the writer and seed the flow id counter from disk.

        Remembers the flows dir path but does not create it; the directory
        (and the data dir) appear lazily on the first flow allocation.
        """
        self._flows_dir = _flows_dir()
        self._counter = itertools.count(_scan_max_flow_id(self._flows_dir) + 1)
        self._jsonl_path = self._flows_dir / "flows.jsonl"

    def _ensure_flows_dir(self) -> None:
        """Create the flows directory (and parents) if missing.

        Called on the first flow allocation so the data dir appears lazily
        on first use, not at server boot.
        """
        self._flows_dir.mkdir(parents=True, exist_ok=True)

    def alloc_flow_id(self) -> str:
        """Allocate a unique zero-padded flow id by atomically creating its dir.

        Uses ``mkdir`` as the uniqueness primitive so multiple processes can
        allocate concurrently without a lock: the filesystem picks one winner
        per id, and losers bump the counter and retry. Ids are unique but not
        contiguous under contention (acceptable — sortability matters, not
        density).

        Returns:
            A zero-padded flow id string matching the created directory name.
        """
        self._ensure_flows_dir()
        while True:
            flow_id = f"{next(self._counter):05d}"
            try:
                (self._flows_dir / flow_id).mkdir()
            except FileExistsError:
                continue
            return flow_id

    def write_request(self, flow_id: str, request_bytes: bytes) -> None:
        """Write the raw request bytes for a flow as a read-only file.

        Args:
            flow_id: Flow id from :meth:`alloc_flow_id`.
            request_bytes: Raw HTTP request bytes (request line + headers +
                blank line + body), CRLF-terminated.
        """
        _write_readonly(self._flows_dir / flow_id / "request", request_bytes)

    def write_meta(self, flow_id: str, *, scheme: str, host: str, port: int) -> None:
        """Write a read-only ``meta.json`` sidecar into a flow directory.

        Holds the connection-level scheme/host/port so ``request clone`` can
        copy it in O(1) instead of scanning ``flows.jsonl``.

        Args:
            flow_id: Flow id.
            scheme: Request scheme (``http``/``https``).
            host: Request host (without port).
            port: Request port.
        """
        data = json.dumps(
            {"scheme": scheme, "host": host, "port": port},
            ensure_ascii=False,
        ).encode("utf-8")
        _write_readonly(self._flows_dir / flow_id / "meta.json", data)

    def write_response(
        self,
        *,
        flow_id: str,
        meta: RequestMeta,
        status_code: int,
        response_headers_bytes: bytes,
        body_bytes: bytes | None,
        content_type: str | None,
        total_duration_ms: float | None,
        keep_body: bool = False,
        browser_id: str | None = None,
    ) -> dict[str, Any]:
        """Write response files and append the jsonl line for a completed flow.

        Args:
            flow_id: Flow id.
            meta: Request metadata (method, scheme, host, port, path).
            status_code: HTTP status code.
            response_headers_bytes: Reconstructed response headers (status
                line + headers + blank line), CRLF-terminated, no body.
            body_bytes: Decoded response body, or ``None`` to skip.
            content_type: ``Content-Type`` header value, used to pick the
                body file extension and decide whether to store the body.
            total_duration_ms: Total request+response duration in
                milliseconds, or ``None``.
            keep_body: If True, store the body even when ``content_type`` is
                in the excluded set (images/video/audio/fonts). Used by
                ``request_send`` (which always passes True) where the
                body is the payload the caller asked for; the
                browser-capture addon path never sets this.
            browser_id: Browser identifier token when the flow rode a
                browser's proxy credentials (ADR-0031); ``None`` for
                non-browser traffic and records written before this
                field existed.

        Returns:
            The flows.jsonl record that was appended.
        """
        flow_dir = self._flows_dir / flow_id

        body_file: str | None = None
        if body_bytes is not None and (keep_body or should_store_body(content_type)):
            ext = _mime_to_ext(content_type)
            body_name = f"response_body{ext}"
            _write_readonly(flow_dir / body_name, body_bytes)
            body_file = f"flows/{flow_id}/{body_name}"

        _write_readonly(flow_dir / "response_headers", response_headers_bytes)

        record = {
            "id": flow_id,
            "method": meta.method,
            "scheme": meta.scheme,
            "host": meta.host,
            "port": meta.port,
            "path": meta.path,
            "browser_id": browser_id,
            "status_code": status_code,
            "total_duration_ms": total_duration_ms,
            "body_file": body_file,
            "error": None,
        }
        self._append_jsonl(record)
        return record

    def write_error(
        self,
        *,
        flow_id: str,
        meta: RequestMeta,
        error_msg: str,
        browser_id: str | None = None,
    ) -> dict[str, Any]:
        """Write an error file and append the jsonl line for a failed flow.

        Args:
            flow_id: Flow id.
            meta: Request metadata.
            error_msg: Error message describing the failure.
            browser_id: Browser identifier token when the flow rode a
                browser's proxy credentials (ADR-0031); ``None``
                otherwise.

        Returns:
            The flows.jsonl record that was appended.
        """
        flow_dir = self._flows_dir / flow_id
        _write_readonly(flow_dir / "error", error_msg.encode("utf-8"))
        record = {
            "id": flow_id,
            "method": meta.method,
            "scheme": meta.scheme,
            "host": meta.host,
            "port": meta.port,
            "path": meta.path,
            "browser_id": browser_id,
            "status_code": None,
            "total_duration_ms": None,
            "body_file": None,
            "error": error_msg,
        }
        self._append_jsonl(record)
        return record

    def _append_jsonl(self, record: dict[str, Any]) -> None:
        """Append one record to flows.jsonl atomically (O_APPEND)."""
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(line)


@dataclass(frozen=True, slots=True)
class RequestMeta:
    """Metadata describing the request side of a flow.

    Used to bundle request fields when writing flow records so writer methods
    stay under the argument limit.
    """

    method: str
    scheme: str
    host: str
    port: int
    path: str


_writer: FlowRecordWriter | None = None


def get_writer() -> FlowRecordWriter:
    """Return the shared :class:`FlowRecordWriter`, creating it on first use."""
    global _writer
    if _writer is None:
        _writer = FlowRecordWriter()
    return _writer


class FlowFileAddon:
    """Mitmproxy addon that stores flows as read-only files on disk."""

    def __init__(self) -> None:
        """Initialize the addon and bind to the shared flow record writer."""
        self._writer = get_writer()
        # flow.id -> (flow_id, browser_id token or None). The token comes
        # from the auth addon's proxyauth metadata stamp (ADR-0031).
        self._pending_flows: dict[int, tuple[str, str | None]] = {}

    def request(self, flow) -> None:
        """Handle request - create flow dir and write the request + meta files.

        The flow dir, request file, and meta.json sidecar are durable
        immediately, even if no response ever arrives. The flows.jsonl line
        is appended only when the flow completes (response or error).

        Flows tagged ``AUTH_CHALLENGE_TAG`` are the auth addon's own canary
        seeding exchanges (proxy.py) — plumbing, not target traffic — and
        are dropped before anything is allocated.

        Args:
            flow: mitmproxy flow object.
        """
        if flow.metadata.get(AUTH_CHALLENGE_TAG):
            return
        auth = flow.metadata.get(PROXYAUTH_METADATA_KEY)
        browser_id: str | None = auth[0] if auth else None
        flow_id = self._writer.alloc_flow_id()
        self._writer.write_request(flow_id, _build_request_bytes(flow))
        self._writer.write_meta(
            flow_id,
            scheme=flow.request.scheme,
            host=flow.request.host,
            port=flow.request.port,
        )
        self._pending_flows[flow.id] = (flow_id, browser_id)

    def response(self, flow) -> None:
        """Handle response - write response files and append the jsonl line.

        Args:
            flow: mitmproxy flow object.
        """
        pending = self._pending_flows.pop(flow.id, None)
        if pending is None:
            return
        flow_id, browser_id = pending

        response = flow.response
        request = flow.request

        content_type = response.headers.get("Content-Type", "")
        body_bytes = response.content if should_store_body(content_type) else None

        total_duration_ms: float | None = None
        if response.timestamp_end and request.timestamp_start:
            elapsed = response.timestamp_end - request.timestamp_start
            total_duration_ms = elapsed * 1000

        meta = RequestMeta(
            method=request.method,
            scheme=request.scheme,
            host=request.host,
            port=request.port,
            path=request.path,
        )
        self._writer.write_response(
            flow_id=flow_id,
            meta=meta,
            status_code=response.status_code,
            response_headers_bytes=_build_response_headers_bytes(flow),
            body_bytes=body_bytes,
            content_type=content_type,
            total_duration_ms=total_duration_ms,
            browser_id=browser_id,
        )

    def error(self, flow) -> None:
        """Handle flow errors - write an error file and append the jsonl line.

        Args:
            flow: mitmproxy flow object with ``flow.error`` populated.
        """
        pending = self._pending_flows.pop(flow.id, None)
        if pending is None:
            return
        flow_id, browser_id = pending

        msg = flow.error.msg if flow.error else "unknown error"
        meta = RequestMeta(
            method=flow.request.method,
            scheme=flow.request.scheme,
            host=flow.request.host,
            port=flow.request.port,
            path=flow.request.path,
        )
        self._writer.write_error(
            flow_id=flow_id, meta=meta, error_msg=msg, browser_id=browser_id
        )
