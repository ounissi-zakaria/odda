"""ASGI app serving dynamic ``?body=&status=&header=&gzip=1`` responses.

Served by hypercorn in e2e tests over HTTPS with HTTP/1.1 and HTTP/2
via ALPN. Replaces the remote xs2.top server so the test suite runs
offline without flakiness from upstream latency jitter.

Run via the `_lib/dyn-server.md` helper, which launches hypercorn
bound to ``127.0.0.1:<port>`` with a self-signed cert and this app.
"""

from __future__ import annotations

import gzip
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Serve one dynamic response per request, driven by the query string."""
    if scope["type"] != "http":
        return
    raw_qs = scope.get("query_string", b"")
    qd = urllib.parse.parse_qs(
        raw_qs.decode() if raw_qs else "", keep_blank_values=True
    )
    # `?race=<id>` records this request's arrival time (monotonic, ms) to
    # a file named by the id, so a test can assert N concurrent requests
    # arrived in a tight window (the single-packet property). The file is
    # one timestamp per line, in arrival order.
    race_id = qd.get("race", [None])[0]
    if race_id:
        race_dir = Path(os.environ.get("ODDA_RACE_DIR", "/tmp/odda-race"))
        race_dir.mkdir(parents=True, exist_ok=True)
        with (race_dir / race_id).open("a") as f:
            f.write(f"{time.monotonic_ns()}\n")
    # Consume the request body before responding. An ASGI app that returns
    # without reading it lets hypercorn complete and drop the stream while the
    # client's trailing DATA frames are still in flight; hypercorn's H2 event
    # dispatch then indexes the removed stream and dies with KeyError,
    # tearing down the whole connection mid-response. odda reports that
    # faithfully as "connection closed before response complete", which is how
    # the H2 repeat tests flaked under CPU load. Reading the body keeps the
    # stream alive until the client's end_stream.
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        if not message.get("more_body", False):
            break
    body = qd.get("body", [""])[0].encode()
    status = int(qd.get("status", ["200"])[0])
    headers: list[tuple[bytes, bytes]] = []
    for h in qd.get("header", []):
        if ":" in h:
            name, value = h.split(":", 1)
            headers.append((name.strip().lower().encode(), value.strip().encode()))
    if not any(name == b"content-type" for name, _ in headers):
        headers.append((b"content-type", b"text/plain; charset=utf-8"))
    if "gzip" in qd:
        body = gzip.compress(body)
        headers.append((b"content-encoding", b"gzip"))
    headers.append((b"content-length", str(len(body)).encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
