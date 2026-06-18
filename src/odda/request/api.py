"""Public API: clone, new, send for editable raw HTTP requests."""

from __future__ import annotations

import json
import shutil
import time
from typing import Any

from odda import flowstore
from odda.flowstore import RequestMeta
from odda.request.h1 import send_h1
from odda.request.h2 import send_h2
from odda.request.parsing import (
    build_request_bytes,
    fix_content_length as fix_cl,
    parse_request,
)
from odda.request.response import decode_content_encoding
from odda.request.storage import read_meta, requests_dir, resolve_name_dir, write_meta
from odda.request.types import REQUEST_FILENAME, EditableMeta


def clone(flow_id: str, name: str, *, force: bool = False) -> dict[str, Any]:
    """Clone a captured flow's request into an editable request.

    Copies ``<DATA_DIR>/flows/<flow_id>/request`` and ``meta.json`` into
    ``<DATA_DIR>/requests/<name>/``. The Host header's explicit port takes
    precedence over the sidecar's port.

    For flows captured before the ``meta.json`` sidecar existed, falls back
    to scanning ``flows.jsonl`` for scheme/port.

    Raises:
        ValueError: If the flow or its request file is not found, or if the
            name already exists without ``force``.
    """
    req_dir = resolve_name_dir(name, force=force)

    flows_dir = flowstore.DATA_DIR / "flows"
    src_request = flows_dir / flow_id / REQUEST_FILENAME
    if not src_request.exists():
        msg = f"Flow {flow_id} not found or has no request file"
        raise ValueError(msg)

    shutil.copy2(src_request, req_dir / REQUEST_FILENAME)
    # Clone source is read-only (0444) in .odda/flows/; make the copy writable
    # so the agent's edit tool can modify it.
    (req_dir / REQUEST_FILENAME).chmod(0o644)

    # Read scheme/host/port from the per-flow meta.json sidecar. These are
    # the TCP destination; the Host header in the request file is left
    # untouched and goes on the wire verbatim (they may intentionally differ
    # for vhost/host-header/SSRF tests).
    src_meta = flows_dir / flow_id / "meta.json"
    if not src_meta.exists():
        msg = (
            f"Flow {flow_id} has no meta.json sidecar. "
            "This flow was captured by an older odda version; re-capture it."
        )
        raise ValueError(msg)
    meta_data = json.loads(src_meta.read_text(encoding="utf-8"))
    scheme = meta_data.get("scheme", "https")
    host = meta_data.get("host", "")
    port = int(meta_data.get("port", 443))

    meta = EditableMeta(scheme=scheme, host=host, port=port)
    write_meta(req_dir, meta)

    return {
        "name": name,
        "path": str(req_dir),
        "flow_id": flow_id,
        "scheme": scheme,
        "host": host,
        "port": port,
    }


def new(
    name: str,
    *,
    host: str,
    protocol: str = "https",
    port: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Create a new empty editable request with a meta sidecar.

    Args:
        name: Editable request name.
        host: Target host (required).
        protocol: ``http`` or ``https`` (default ``https``).
        port: Target port. Defaults to 80 for http, 443 for https.
        force: Overwrite an existing request of the same name.
    """
    scheme = protocol.lower()
    if scheme not in ("http", "https"):
        msg = f"protocol must be 'http' or 'https', got {protocol!r}"
        raise ValueError(msg)
    if port is None:
        port = 443 if scheme == "https" else 80

    req_dir = resolve_name_dir(name, force=force)
    (req_dir / REQUEST_FILENAME).write_bytes(b"")
    meta = EditableMeta(scheme=scheme, host=host, port=port)
    write_meta(req_dir, meta)
    return {
        "name": name,
        "path": str(req_dir),
        "scheme": scheme,
        "host": host,
        "port": port,
    }


async def send(
    name: str,
    *,
    fix_content_length: bool = False,
    timeout: float = 30.0,
    insecure: bool = False,
) -> dict[str, Any]:
    """Send an editable request and write a flow record.

    Reads ``<DATA_DIR>/requests/<name>/request`` + ``meta.json``, opens a
    socket, sends the bytes raw, reads the response, decodes it, and writes
    a flow record to ``<DATA_DIR>/flows/`` via the shared
    :class:`~odda.flowstore.FlowRecordWriter`.

    The request file is written to the flow directory before sending
    (two-phase durability). On any error (timeout, DNS, TLS, connection),
    an error flow record is written instead.

    Returns:
        The ``flows.jsonl`` record that was appended.
    """
    req_dir = requests_dir() / name
    if not req_dir.exists():
        msg = f"Request '{name}' not found"
        raise ValueError(msg)

    meta = read_meta(req_dir)
    request_bytes = (req_dir / REQUEST_FILENAME).read_bytes()
    if not request_bytes:
        msg = "Request file is empty"
        raise ValueError(msg)

    parsed = parse_request(request_bytes)

    if fix_content_length:
        parsed = fix_cl(parsed)
        request_bytes = build_request_bytes(parsed)

    is_h2 = parsed.version.upper().startswith("HTTP/2")

    rmeta = RequestMeta(
        method=parsed.method,
        scheme=meta.scheme,
        host=meta.host,
        port=meta.port,
        path=parsed.path,
    )

    writer = flowstore.get_writer()
    flow_id = writer.alloc_flow_id()
    writer.write_request(flow_id, request_bytes)
    writer.write_meta(flow_id, scheme=meta.scheme, host=meta.host, port=meta.port)

    start_time = time.monotonic()

    try:
        if is_h2:
            raw = await send_h2(
                meta.host,
                meta.port,
                meta.scheme,
                parsed,
                timeout=timeout,
                insecure=insecure,
            )
        else:
            raw = await send_h1(
                meta.host,
                meta.port,
                meta.scheme,
                parsed.method,
                request_bytes,
                timeout=timeout,
                insecure=insecure,
                has_body=bool(parsed.body),
                has_content_length=parsed.has_content_length,
                has_transfer_encoding=parsed.has_transfer_encoding,
            )

        decoded_body = decode_content_encoding(raw.body_bytes, raw.content_encoding)

        duration_ms = (time.monotonic() - start_time) * 1000
        return writer.write_response(
            flow_id=flow_id,
            meta=rmeta,
            status_code=raw.status_code,
            response_headers_bytes=raw.headers_bytes,
            body_bytes=decoded_body,
            content_type=raw.content_type,
            total_duration_ms=duration_ms,
        )

    except Exception as exc:
        error_msg = str(exc) or type(exc).__name__
        return writer.write_error(
            flow_id=flow_id,
            meta=rmeta,
            error_msg=error_msg,
        )
