"""Public API: clone, new, send for editable raw HTTP requests."""

from __future__ import annotations

import json
import shutil
import time
from typing import Any

from odda import flowstore
from odda.flowstore import RequestMeta
from odda.request.h1 import PipelineError, send_h1, send_h1_parallel, send_h1_pipeline
from odda.request.h2 import ConcurrentConnectionError, send_h2, send_h2_concurrent
from odda.request.parsing import fix_content_length_bytes, parse_request
from odda.request.response import decode_content_encoding
from odda.request.storage import read_meta, requests_dir, resolve_name_dir, write_meta
from odda.request.types import (
    REQUEST_FILENAME,
    ConcurrentResult,
    EditableMeta,
    ParsedRequest,
    StreamError,
)


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
    line_terminator: bytes = b"\r\n",
    force: bool = False,
) -> dict[str, Any]:
    r"""Create a new empty editable request with a meta sidecar.

    Args:
        name: Editable request name.
        host: Target host (required).
        protocol: ``http`` or ``https`` (default ``https``).
        port: Target port. Defaults to 80 for http, 443 for https.
        line_terminator: Byte sequence the request parser splits header
            lines on (default ``\r\n``). Only honored for HTTP/2 request
            files; lets an agent put a literal CRLF inside an H2 header
            value (H2→H1 downgrade smuggling) by setting it to ``\n``.
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
    meta = EditableMeta(
        scheme=scheme, host=host, port=port, line_terminator=line_terminator
    )
    write_meta(req_dir, meta)
    return {
        "name": name,
        "path": str(req_dir),
        "scheme": scheme,
        "host": host,
        "port": port,
    }


def _looks_like_h2(request_bytes: bytes) -> bool:
    r"""Sniff whether the request line says ``HTTP/2`` without full parsing.

    The version token lives in the request line at the very start of the
    file. Rather than trying to find the end of the first line (which
    requires knowing the line terminator — and we're sniffing to decide
    whether to honor the custom terminator), check whether ``HTTP/2``
    appears as the version token in the leading bytes. ``HTTP/1.1`` and
    ``HTTP/1.0`` do not contain ``HTTP/2``, so a substring check on the
    first chunk is unambiguous.
    """
    head = request_bytes[:4096]
    return b" HTTP/2" in head or head.startswith(b"HTTP/2")


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

    The response body is always stored, regardless of ``Content-Type``.
    The image/video/audio/font exclusion policy in
    :func:`~odda.flowstore.should_store_body` exists for browser-captured
    sub-resources (telemetry, tracking pixels); a hand-built raw request
    exists to see its response body (e.g. a path-traversal file mislabeled
    ``image/jpeg``), so ``keep_body=True`` is passed to the writer.

    Args:
        name: Editable request name.
        fix_content_length: Recompute ``Content-Length`` from the body.
        timeout: Total timeout in seconds.
        insecure: Skip TLS certificate verification.

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

    is_h2 = _looks_like_h2(request_bytes)
    lt = meta.line_terminator if is_h2 else b"\r\n"

    parsed = parse_request(request_bytes, line_terminator=lt)

    if fix_content_length:
        request_bytes = fix_content_length_bytes(request_bytes, line_terminator=lt)
        # Re-parse so the H2 path (which builds wire frames from ``parsed``
        # headers, not ``request_bytes``) sees the corrected Content-Length
        # and body-length match. The H1 path sends ``request_bytes`` raw,
        # so it already carries the fix.
        parsed = parse_request(request_bytes, line_terminator=lt)

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
            keep_body=True,
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

    Args:
        names: Two or more editable request names.
        fix_content_length: Rejected (raises) for multi-name sends.
        timeout: Total timeout in seconds for connect + all sends + reads.
        insecure: Skip TLS certificate verification.
        pipelining: True H1 pipelining (send all, then read all) instead of
            sequential keep-alive (send-then-read per request).

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

    # Load + parse every request up front so pre-emptive rejections fire
    # before any flow id is allocated or any socket opens. A failure here
    # raises with no side effects.
    loaded: list[tuple[str, bytes, ParsedRequest, EditableMeta]] = []
    is_h2_all = True
    is_h1_all = True
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
        is_h2_req = _looks_like_h2(request_bytes)
        parsed = parse_request(
            request_bytes,
            line_terminator=meta.line_terminator if is_h2_req else b"\r\n",
        )
        if is_h2_req:
            is_h1_all = False
        else:
            is_h2_all = False
        # Bare-body rejection applies to the H1 pipeline path only (it
        # requires write_eof, ending the connection). H2 concurrent uses
        # stream framing, so a body without Content-Length/TE is fine.
        if (
            not parsed.version.upper().startswith("HTTP/2")
            and bool(parsed.body)
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

    if not is_h2_all and not is_h1_all:
        msg = (
            "multi-name send cannot mix HTTP/1.1 and HTTP/2 request lines; "
            "use all HTTP/2 for concurrent stream-multiplex (ADR-0020) or "
            "all HTTP/1.1 for the sequential pipeline"
        )
        raise ValueError(msg)

    if is_h2_all and pipelining:
        msg = (
            "pipelining is HTTP/1.1 multi-name only (send-all-then-read-all "
            "sequential); HTTP/2 multi-name is already concurrent "
            "stream-multiplex — drop pipelining for H2"
        )
        raise ValueError(msg)

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
            keep_body=True,
        )

    try:
        if is_h2_all:
            # H2 multi-name: concurrent stream-multiplex (ADR-0020), the
            # multi-endpoint race path. send_h2_concurrent returns one
            # ConcurrentResult (RawResponse | StreamError) per request in
            # send order; per-stream errors are isolated. A connection-
            # level error raises ConcurrentConnectionError carrying the
            # partial results.
            parsed_list = [pr for _n, _rb, pr, _m in loaded]
            try:
                concurrent_raws = await send_h2_concurrent(
                    first_meta.host,
                    first_meta.port,
                    first_meta.scheme,
                    parsed_list,
                    timeout=timeout,
                    insecure=insecure,
                )
            except ConcurrentConnectionError as exc:
                concurrent_raws = exc.results
            return _write_concurrent_results(
                prewritten, concurrent_raws, writer, start_time
            )
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


def _write_concurrent_results(
    prewritten: list[tuple[str, str, RequestMeta, ParsedRequest]],
    concurrent_raws: list[ConcurrentResult],
    writer: Any,
    start_time: float,
) -> list[dict[str, Any]]:
    """Write flow records for a concurrent-send result list.

    Each slot is a :class:`RawResponse` (write a response flow) or a
    :class:`StreamError` (write an error flow). Per-stream errors are
    isolated; ``flows.jsonl`` stays complete — N pre-written requests, N
    lines (ADR-0020).
    """
    records: list[dict[str, Any]] = []
    for (flow_id, _name, rmeta, _parsed), result in zip(
        prewritten, concurrent_raws, strict=True
    ):
        if isinstance(result, StreamError):
            records.append(
                writer.write_error(flow_id=flow_id, meta=rmeta, error_msg=result.error)
            )
        else:
            decoded_body = decode_content_encoding(
                result.body_bytes, result.content_encoding
            )
            duration_ms = (time.monotonic() - start_time) * 1000
            records.append(
                writer.write_response(
                    flow_id=flow_id,
                    meta=rmeta,
                    status_code=result.status_code,
                    response_headers_bytes=result.headers_bytes,
                    body_bytes=decoded_body,
                    content_type=result.content_type,
                    total_duration_ms=duration_ms,
                    keep_body=True,
                )
            )
    return records


async def send_repeat(
    name: str,
    repeat: int,
    *,
    fix_content_length: bool = False,
    timeout: float = 30.0,
    insecure: bool = False,
) -> list[dict[str, Any]]:
    """Send one editable request N times concurrently (ADR-0020).

    Same request, N copies, all at once — the race / limit-overrun
    pattern. HTTP/2 uses stream multiplexing with the last-byte
    single-packet technique (all N HEADERS frames in one TLS record) so
    the requests arrive near-simultaneously; HTTP/1.1 opens N parallel
    connections (H1 cannot multiplex on one connection, and pipelining is
    server-side sequential).

    All N flow records (one-request-one-response, ADR-0019 invariant)
    are pre-written before the socket opens: N identical request files,
    N ``meta.json`` sidecars, N ``flows.jsonl`` lines. The agent counts
    successes by reading the N flows' ``status_code``.

    Pre-emptive rejections (before the socket opens):

    - ``repeat`` < 2 → error (use single-name ``send`` for one request).
    - ``pipelining`` is rejected (``repeat`` is concurrent;
      ``pipelining`` is H1 multi-name sequential).

    Error policy (ADR-0020): per-stream errors are isolated (one stream's
    failure does not abort the others); a connection-level error (H2
    GOAWAY, TLS drop) aborts all not-yet-complete streams.

    Args:
        name: Editable request name (single-name only).
        repeat: Number of concurrent copies (>= 2).
        fix_content_length: Recompute Content-Length from the body once
            before sending (allowed: the race is never a CL-differential
            attack; ADR-0019's multi-name rejection rationale does not
            transfer).
        timeout: Total timeout for connect + all sends + all reads.
        insecure: Skip TLS certificate verification.

    Returns:
        A list of ``flows.jsonl`` records (one per copy, in send order).
    """
    if repeat < 2:
        msg = (
            f"repeat must be >= 2 (got {repeat}); for a single request "
            "use `request_send` with one name and no repeat"
        )
        raise ValueError(msg)

    req_dir = requests_dir() / name
    if not req_dir.exists():
        msg = f"Request '{name}' not found"
        raise ValueError(msg)

    meta = read_meta(req_dir)
    request_bytes = (req_dir / REQUEST_FILENAME).read_bytes()
    if not request_bytes:
        msg = "Request file is empty"
        raise ValueError(msg)

    is_h2 = _looks_like_h2(request_bytes)
    lt = meta.line_terminator if is_h2 else b"\r\n"
    parsed = parse_request(request_bytes, line_terminator=lt)

    if fix_content_length:
        request_bytes = fix_content_length_bytes(request_bytes, line_terminator=lt)
        parsed = parse_request(request_bytes, line_terminator=lt)

    rmeta = RequestMeta(
        method=parsed.method,
        scheme=meta.scheme,
        host=meta.host,
        port=meta.port,
        path=parsed.path,
    )

    writer = flowstore.get_writer()

    # Pre-write all N flow records (identical request files + meta
    # sidecars) before opening any socket. On crash, all N request files
    # are durable (ADR-0019 two-phase durability scaled to N).
    prewritten: list[tuple[str, RequestMeta]] = []
    for _ in range(repeat):
        flow_id = writer.alloc_flow_id()
        writer.write_request(flow_id, request_bytes)
        writer.write_meta(flow_id, scheme=meta.scheme, host=meta.host, port=meta.port)
        prewritten.append((flow_id, rmeta))

    start_time = time.monotonic()

    try:
        if is_h2:
            concurrent_raws = await send_h2_concurrent(
                meta.host,
                meta.port,
                meta.scheme,
                [parsed for _ in range(repeat)],
                timeout=timeout,
                insecure=insecure,
            )
        else:
            concurrent_raws = await send_h1_parallel(
                meta.host,
                meta.port,
                meta.scheme,
                [(request_bytes, parsed) for _ in range(repeat)],
                timeout=timeout,
                insecure=insecure,
            )
    except ConcurrentConnectionError as exc:
        concurrent_raws = exc.results
    except Exception as exc:
        # Pre-flight failure (connect timeout before any stream opened).
        # Mark all N flows as errored. flows.jsonl stays complete.
        error_msg = str(exc) or type(exc).__name__
        return [
            writer.write_error(flow_id=flow_id, meta=rm, error_msg=error_msg)
            for flow_id, rm in prewritten
        ]

    return _write_concurrent_results(
        [(flow_id, name, rm, parsed) for (flow_id, rm) in prewritten],
        concurrent_raws,
        writer,
        start_time,
    )
