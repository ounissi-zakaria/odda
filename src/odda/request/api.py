"""Public API: clone, new, send for editable raw HTTP requests."""

from __future__ import annotations

import json
import shutil
import time
from typing import Any

from odda import flowstore
from odda.flowstore import RequestMeta
from odda.request.h1 import PipelineError, send_h1, send_h1_pipeline
from odda.request.h2 import send_h2
from odda.request.parsing import fix_content_length_bytes, parse_request
from odda.request.response import decode_content_encoding
from odda.request.storage import read_meta, requests_dir, resolve_name_dir, write_meta
from odda.request.types import REQUEST_FILENAME, EditableMeta, ParsedRequest


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
        request_bytes = fix_content_length_bytes(request_bytes)

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


async def send_pipeline(
    names: list[str],
    *,
    fix_content_length: bool = False,
    timeout: float = 30.0,
    insecure: bool = False,
    pipelining: bool = False,
) -> list[dict[str, Any]]:
    """Send multiple editable requests on one HTTP/1.1 connection.

    Multi-name mode of ``request send``: opens one connection (TLS for
    https, ALPN http/1.1), sends each request's exact bytes, reads each
    response, and writes one flow record per request. The connection stays
    open across sends (sequential keep-alive by default; ``pipelining``
    writes all requests before reading any response). All N request files
    and ``meta.json`` sidecars are pre-written before the socket opens
    (two-phase durability scaled to N), so a crash leaves a durable record
    of every attempted request.

    Pre-emptive rejections (before the socket opens):

    - ``fix_content_length`` with multiple names — it would overwrite the
      intentionally-wrong ``Content-Length`` that smuggling payloads depend
      on.
    - Any request whose request line says ``HTTP/2`` — H1-style smuggling is
      meaningless in pure H2; multi-name is an H1-only feature.
    - Any request with a body but no ``Content-Length`` and no
      ``Transfer-Encoding`` — it would require ``write_eof`` (half-close),
      ending the connection. Checked pre-emptively here (before flow
      allocation) and defensively in :func:`send_h1_pipeline` (for
      direct callers).

    Error policy: if step N fails (timeout, connection drop), step N gets
    today's error flow; steps N+1..end each get an ``"aborted: step N
    failed"`` error flow (their pre-written request files already exist);
    the connection closes unconditionally. ``flows.jsonl`` stays complete
    — every pre-written request has a corresponding line.

    Returns:
        A list of ``flows.jsonl`` records (one per request), in send order.
    """
    if fix_content_length:
        msg = (
            "--fix-content-length mutates request bytes and would overwrite "
            "the intentionally-wrong Content-Length that smuggling payloads "
            "depend on; send each request separately with --fix-content-length "
            "or omit the flag for multi-name send"
        )
        raise ValueError(msg)

    if len(names) < 2:
        msg = "send_pipeline requires at least two names"
        raise ValueError(msg)

    writer = flowstore.get_writer()

    # Load + parse every request up front so pre-emptive rejections (H2,
    # missing request) fire before any flow id is allocated or any socket
    # opens. A failure here raises with no side effects.
    loaded: list[tuple[str, bytes, ParsedRequest, EditableMeta]] = []
    for name in names:
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
        if parsed.version.upper().startswith("HTTP/2"):
            msg = (
                "multi-name send is HTTP/1.1 only; H2 smuggling is a "
                "different attack class, use single-name send for H2"
            )
            raise ValueError(msg)
        if (
            bool(parsed.body)
            and not parsed.has_content_length
            and not parsed.has_transfer_encoding
        ):
            msg = (
                "request has a body with no Content-Length and no "
                "Transfer-Encoding, which requires half-closing the socket "
                "and is incompatible with multi-name send"
            )
            raise ValueError(msg)
        loaded.append((name, request_bytes, parsed, meta))

    # Pre-write all N flow records (request files + meta sidecars) before
    # opening the socket. On crash mid-pipeline, all N request files are
    # durable; response files exist only for responses actually received.
    prewritten: list[tuple[str, str, RequestMeta, ParsedRequest]] = []
    for name, request_bytes, parsed, meta in loaded:
        flow_id = writer.alloc_flow_id()
        writer.write_request(flow_id, request_bytes)
        writer.write_meta(flow_id, scheme=meta.scheme, host=meta.host, port=meta.port)
        rmeta = RequestMeta(
            method=parsed.method,
            scheme=meta.scheme,
            host=meta.host,
            port=meta.port,
            path=parsed.path,
        )
        prewritten.append((flow_id, name, rmeta, parsed))

    start_time = time.monotonic()
    requests_payload = [(rb, pr) for _n, rb, pr, _m in loaded]
    first_meta = loaded[0][3]

    def _write_response_record(
        flow_id: str, rmeta: RequestMeta, raw: object
    ) -> dict[str, Any]:
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

    try:
        raws = await send_h1_pipeline(
            first_meta.host,
            first_meta.port,
            first_meta.scheme,
            requests_payload,
            timeout=timeout,
            insecure=insecure,
            pipelining=pipelining,
        )
    except PipelineError as exc:
        # A mid-sequence failure. Write response flows for the steps that
        # succeeded (0..failed_step-1), an error flow for the failed step,
        # and aborted flows for the rest. flows.jsonl stays complete.
        records: list[dict[str, Any]] = []
        for i, raw in enumerate(exc.partial_responses):
            flow_id, _name, rmeta, _parsed = prewritten[i]
            records.append(_write_response_record(flow_id, rmeta, raw))
        failed_step = exc.failed_step
        error_msg = str(exc) or type(exc).__name__
        flow_id, _name, rmeta, _parsed = prewritten[failed_step]
        records.append(
            writer.write_error(flow_id=flow_id, meta=rmeta, error_msg=error_msg)
        )
        abort_msg = f"aborted: step {failed_step + 1} failed ({error_msg})"
        for flow_id, _name, rmeta, _parsed in prewritten[failed_step + 1 :]:
            records.append(
                writer.write_error(flow_id=flow_id, meta=rmeta, error_msg=abort_msg)
            )
        return records
    except Exception as exc:
        # Pre-flight failure (bare-body ValueError, connect timeout before
        # any step ran). No partial responses; attribute the failure to
        # step 1 and abort the rest.
        error_msg = str(exc) or type(exc).__name__
        records: list[dict[str, Any]] = []
        flow_id, _name, rmeta, _parsed = prewritten[0]
        records.append(
            writer.write_error(flow_id=flow_id, meta=rmeta, error_msg=error_msg)
        )
        abort_msg = f"aborted: step 1 failed ({error_msg})"
        for flow_id, _name, rmeta, _parsed in prewritten[1:]:
            records.append(
                writer.write_error(flow_id=flow_id, meta=rmeta, error_msg=abort_msg)
            )
        return records

    # Success: write all N response records in send order.
    return [
        _write_response_record(flow_id, rmeta, raw)
        for (flow_id, _name, rmeta, _parsed), raw in zip(prewritten, raws, strict=True)
    ]
