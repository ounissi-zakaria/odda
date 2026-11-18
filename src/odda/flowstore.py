from __future__ import annotations

import contextlib
import itertools
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

EXCLUDED_CONTENT_TYPES: tuple[str, ...] = (
    "font/",
    "video/",
    "audio/",
    "image/",
)

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

# See ADR-0031.
PROXYAUTH_METADATA_KEY = "proxyauth"

AUTH_CHALLENGE_TAG = "odda_auth_challenge"


def set_data_dir(path: Path | str) -> None:
    global DATA_DIR, _writer
    DATA_DIR = Path(path)
    _writer = None


def _flows_dir() -> Path:
    return DATA_DIR / "flows"


def should_store_body(content_type: str | None) -> bool:
    return not content_type or not content_type.startswith(EXCLUDED_CONTENT_TYPES)


def _mime_to_ext(content_type: str | None) -> str:
    if not content_type:
        return ".bin"
    mime = content_type.split(";")[0].strip().lower()
    return _MIME_TO_EXT.get(mime, ".bin")


def _build_request_bytes(flow) -> bytes:
    # See ADR-0013.
    request = flow.request
    if request.method == "CONNECT":
        target = request.authority or f"{request.host}:{request.port}"
    else:
        target = request.path
    lines = [f"{request.method} {target} {request.http_version}".encode("ascii")]
    for name, value in request.headers.items(multi=True):
        lines.append(f"{name}: {value}".encode("ascii", errors="replace"))

    lines.append(b"")
    lines.append(b"")
    head = b"\r\n".join(lines)
    body = request.content or b""
    return head + body


def _build_response_headers_bytes(flow) -> bytes:
    # See ADR-0013.
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
    path.write_bytes(data)
    with contextlib.suppress(OSError):
        path.chmod(_READ_ONLY)


def _scan_max_flow_id(flows_dir: Path) -> int:
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
    def __init__(self) -> None:
        self._flows_dir = _flows_dir()
        self._counter = itertools.count(_scan_max_flow_id(self._flows_dir) + 1)
        self._jsonl_path = self._flows_dir / "flows.jsonl"
        # See ADR-0034.
        self._observer: Callable[[dict[str, Any]], None] | None = None

    def _ensure_flows_dir(self) -> None:
        self._flows_dir.mkdir(parents=True, exist_ok=True)

    def alloc_flow_id(self) -> str:
        self._ensure_flows_dir()
        while True:
            flow_id = f"{next(self._counter):05d}"
            try:
                (self._flows_dir / flow_id).mkdir()
            except FileExistsError:
                continue
            return flow_id

    def write_request(self, flow_id: str, request_bytes: bytes) -> None:
        _write_readonly(self._flows_dir / flow_id / "request", request_bytes)

    def write_meta(self, flow_id: str, *, scheme: str, host: str, port: int) -> None:
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
        ws: bool = False,
    ) -> dict[str, Any]:
        # See ADR-0031, ADR-0041.
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
        if ws:
            record["ws"] = True
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
        # See ADR-0031.
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

    def write_ws_message(
        self,
        flow_id: str,
        *,
        direction: str,
        opcode: str,
        content: str,
        ts: float,
    ) -> None:
        # See ADR-0041.
        self._append_sidecar(
            flow_id,
            {"from": direction, "opcode": opcode, "content": content, "ts": ts},
        )

    def write_ws_close(
        self,
        flow_id: str,
        *,
        code: int | None,
        reason: str | None,
        closed_by_client: bool,
    ) -> None:
        self._append_sidecar(
            flow_id,
            {
                "type": "close",
                "code": code,
                "reason": reason,
                "closed_by_client": closed_by_client,
            },
        )
        (self._flows_dir / flow_id / "ws.jsonl").chmod(_READ_ONLY)

    def _append_sidecar(self, flow_id: str, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with (self._flows_dir / flow_id / "ws.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line)

    def _append_jsonl(self, record: dict[str, Any]) -> None:
        # See ADR-0034.
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        observer = self._observer
        if observer is not None:
            try:
                observer(record)
            except Exception:
                logger.exception("flow observer failed; write path unaffected")

    def set_observer(self, observer: Callable[[dict[str, Any]], None] | None) -> None:
        self._observer = observer


@dataclass(frozen=True, slots=True)
class RequestMeta:
    method: str
    scheme: str
    host: str
    port: int
    path: str


def _flow_browser_id(flow) -> str | None:
    # See ADR-0031.
    auth = flow.metadata.get(PROXYAUTH_METADATA_KEY)
    return auth[0] if auth else None


def _request_meta(
    flow, *, scheme: str | None = None, path: str | None = None
) -> RequestMeta:
    request = flow.request
    return RequestMeta(
        method=request.method,
        scheme=scheme if scheme is not None else request.scheme,
        host=request.host,
        port=request.port,
        path=path if path is not None else request.path,
    )


_writer: FlowRecordWriter | None = None


def get_writer() -> FlowRecordWriter:
    global _writer
    if _writer is None:
        _writer = FlowRecordWriter()
    return _writer


class FlowFileAddon:
    def __init__(self) -> None:
        self._writer = get_writer()
        # See ADR-0031.
        self._pending_flows: dict[int, tuple[str, str | None]] = {}
        # See ADR-0041.
        self._ws_flows: dict[int, str] = {}

    def request(self, flow) -> None:
        if flow.metadata.get(AUTH_CHALLENGE_TAG):
            return
        browser_id = _flow_browser_id(flow)
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
        pending = self._pending_flows.pop(flow.id, None)
        if pending is None:
            return
        flow_id, browser_id = pending

        response = flow.response
        request = flow.request

        # See ADR-0041.
        ws = flow.websocket is not None
        if ws:
            self._ws_flows[flow.id] = flow_id

        content_type = response.headers.get("Content-Type", "")
        body_bytes = response.content if should_store_body(content_type) else None

        total_duration_ms: float | None = None
        if response.timestamp_end and request.timestamp_start:
            elapsed = response.timestamp_end - request.timestamp_start
            total_duration_ms = elapsed * 1000

        meta = _request_meta(flow)
        self._writer.write_response(
            flow_id=flow_id,
            meta=meta,
            status_code=response.status_code,
            response_headers_bytes=_build_response_headers_bytes(flow),
            body_bytes=body_bytes,
            content_type=content_type,
            total_duration_ms=total_duration_ms,
            browser_id=browser_id,
            ws=ws,
        )

    def websocket_message(self, flow) -> None:
        # See ADR-0041.
        flow_id = self._ws_flows.get(flow.id)
        if flow_id is None:
            return
        message = flow.websocket.messages[-1]
        if message.is_text:
            opcode = "text"
            content = message.content.decode("utf-8", "replace")
        else:
            opcode = "binary"
            content = message.content.decode("latin-1")
        self._writer.write_ws_message(
            flow_id,
            direction="client" if message.from_client else "server",
            opcode=opcode,
            content=content,
            ts=message.timestamp,
        )

    def websocket_end(self, flow) -> None:
        flow_id = self._ws_flows.pop(flow.id, None)
        if flow_id is None:
            return
        websocket = flow.websocket
        self._writer.write_ws_close(
            flow_id,
            code=websocket.close_code,
            reason=websocket.close_reason,
            closed_by_client=websocket.closed_by_client,
        )

    def error(self, flow) -> None:
        pending = self._pending_flows.pop(flow.id, None)
        if pending is None:
            return
        flow_id, browser_id = pending

        msg = flow.error.msg if flow.error else "unknown error"
        meta = _request_meta(flow)
        self._writer.write_error(
            flow_id=flow_id, meta=meta, error_msg=msg, browser_id=browser_id
        )

    def http_connect_error(self, flow) -> None:
        if flow.metadata.get(AUTH_CHALLENGE_TAG):
            return
        msg = _connect_failure_message(flow)
        if msg is None:
            return
        browser_id = _flow_browser_id(flow)
        flow_id = self._writer.alloc_flow_id()
        self._writer.write_request(flow_id, _build_request_bytes(flow))
        self._writer.write_meta(
            flow_id,
            scheme="https",
            host=flow.request.host,
            port=flow.request.port,
        )
        meta = _request_meta(
            flow, scheme="https", path=flow.request.authority or flow.request.path
        )
        self._writer.write_error(
            flow_id=flow_id, meta=meta, error_msg=msg, browser_id=browser_id
        )


_CLIENT_CANCEL_MESSAGES = ("client disconnected", "connection killed")

_CONNECT_502 = re.compile(r"^Cannot connect to .*?: (.*?)(?: If you plan|$)", re.DOTALL)


def _connect_failure_message(flow) -> str | None:
    if flow.error is not None and flow.error.msg:
        msg: str = flow.error.msg
    else:
        msg = ""
        response = flow.response
        if response is not None and response.content:
            match = _CONNECT_502.match(response.get_text(strict=False) or "")
            if match is not None:
                msg = match.group(1)
    if not msg or any(c in msg for c in _CLIENT_CANCEL_MESSAGES):
        return None
    return msg
