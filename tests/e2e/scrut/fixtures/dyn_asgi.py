"""ASGI app serving dynamic ``?body=&status=&header=&gzip=1`` responses.

Served by hypercorn in e2e tests over HTTPS with HTTP/1.1 and HTTP/2
via ALPN. Replaces the remote xs2.top server so the test suite runs
offline without flakiness from upstream latency jitter.

Run via the `_lib/dyn-server.md` helper, which launches hypercorn
bound to ``127.0.0.1:<port>`` with a self-signed cert and this app.
"""

from __future__ import annotations

import gzip
import urllib.parse
from typing import Any


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Serve one dynamic response per request, driven by the query string."""
    if scope["type"] != "http":
        return
    raw_qs = scope.get("query_string", b"")
    qd = urllib.parse.parse_qs(
        raw_qs.decode() if raw_qs else "", keep_blank_values=True
    )
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
