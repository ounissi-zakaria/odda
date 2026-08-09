"""Close-after-first fixture server for `--pipelining` abort tests.

A minimal raw HTTP/1.1 server that, for each accepted TCP connection,
reads exactly one request (until the empty header-terminator line),
writes back one ``200 OK`` response carrying ``Connection: close`` and
the query-string-requested body, then closes the connection. Any bytes
the client wrote after the first request (pipelined requests 2..N) are
discarded when the socket closes. This faithfully models the
real-world smuggling-target behavior that ``--pipelining`` is built
for: the server answers the first request and tears down the
connection, so the pipelined follow-ups never get a response.

Hypercorn (the dyn server) rejects this pattern with a ``400`` because
it parses the trailing pipelined bytes as a malformed request before
emitting its first response. This raw server never reads past the first
request's header terminator, so the first response is always a clean
``200`` regardless of what the client pipelined behind it.

Run as: python3 close_after_first_server.py <port>

Plain HTTP/1.1 (no TLS, no h2) — the pipelining-abort tests only need
the close-after-first behavior, not protocol coverage. The body and
status come from the query string (``?body=<text>&status=<code>``) so
the first response is assertion-friendly.
"""

from __future__ import annotations

import contextlib
import socket
import sys
import urllib.parse

_HEADER_TERMINATOR = b"\r\n\r\n"
_MIN_REQUEST_LINE_PARTS = 2


def _read_headers(sock: socket.socket) -> bytes:
    r"""Read bytes from ``sock`` until ``\r\n\r\n`` ends the headers."""
    buf = b""
    while _HEADER_TERMINATOR not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def _query(buf: bytes) -> dict[str, str]:
    """Parse the ``?body=&status=`` query string from the request line."""
    first_line = buf.split(b"\r\n", 1)[0]
    parts = first_line.split(b" ")
    path = parts[1] if len(parts) >= _MIN_REQUEST_LINE_PARTS else b"/"
    raw_qs = path.split(b"?", 1)[1] if b"?" in path else b""
    return {k: v[0] for k, v in urllib.parse.parse_qs(raw_qs).items()}


def _respond(sock: socket.socket, body: bytes, status: int) -> None:
    """Write one ``200 OK``-style response with ``Connection: close``."""
    head = (
        f"HTTP/1.1 {status} OK\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode()
    with contextlib.suppress(OSError):
        sock.sendall(head + body)


def serve_one(conn: socket.socket) -> None:
    """Read one request, send one response, then close."""
    try:
        buf = _read_headers(conn)
        if not buf:
            return
        q = _query(buf)
        body = q.get("body", "step1-ok").encode()
        status = int(q.get("status", "200"))
        _respond(conn, body, status)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            conn.shutdown(socket.SHUT_RDWR)
        conn.close()


def main() -> None:
    """Listen on 127.0.0.1:<port>, one connection at a time, forever."""
    port = int(sys.argv[1])
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(16)
    while True:
        conn, _ = srv.accept()
        serve_one(conn)


if __name__ == "__main__":
    main()
