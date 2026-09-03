"""Close-without-response fixture server for single-shot error-flow tests.

A minimal raw TCP server that, for each accepted connection, closes
immediately without reading or writing anything. This models a server
that accepts the connection then drops it — the read side hits EOF
before any response headers arrive.

Used to verify that single-shot ``odda request send`` (which shares the
response-header reader with the multi-name pipeline) records a
descriptive error flow (``status_code: null`` + ``error`` naming the
close) instead of the ambiguous ``status_code: 0, error: null``
pseudo-response that previously masked dropped connections.

Run as: python3 close_without_response_server.py <port>

Plain TCP (no TLS, no HTTP) — only the accept-then-close behavior
matters; the client writes a request, the server closes, the read
yields EOF before headers.
"""

from __future__ import annotations

import contextlib
import socket
import sys


def main() -> None:
    """Listen on 127.0.0.1:<port>, accept and close each connection, forever."""
    port = int(sys.argv[1])
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(16)
    while True:
        conn, _ = srv.accept()
        with contextlib.suppress(OSError):
            conn.shutdown(socket.SHUT_RDWR)
        conn.close()


if __name__ == "__main__":
    main()
