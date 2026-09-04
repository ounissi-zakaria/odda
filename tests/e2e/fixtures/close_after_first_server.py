"""Close-after-first fixture server for `pipelining` abort tests.

A minimal raw HTTP/1.1 server that, for each accepted TCP connection,
reads exactly one request (until the empty header-terminator line),
writes back one ``200 OK`` response carrying ``Connection: close`` and
the query-string-requested body, then half-closes the write side and
drains any pipelined follow-up bytes the client wrote behind the first
request before fully closing. This faithfully models the real-world
smuggling-target behavior that ``pipelining`` is built for — the
server answers the first request and tears down the connection, so the
pipelined follow-ups never get a response — while closing cleanly (FIN,
not RST) so the first response is always delivered to the client.

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
import threading
import urllib.parse

_HEADER_TERMINATOR = b"\r\n\r\n"
_MIN_REQUEST_LINE_PARTS = 2
# How long to block waiting for in-flight pipelined bytes to arrive while
# draining the receive buffer before a clean close (see ``_drain_remaining``).
_DRAIN_TIMEOUT = 0.05


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


def _drain_remaining(conn: socket.socket) -> None:
    """Bounded blocking drain of pipelined bytes still in the receive buffer.

    After responding to the first request, read and discard any follow-up
    request bytes the client has pipelined behind it, so the following
    ``shutdown``/``close`` issues a FIN rather than an RST. An RST (sent
    when unread bytes remain in the receive buffer at close time) can
    overtake and discard the first response already queued in the client's
    kernel receive buffer, turning the deterministic "step 1 succeeds,
    step 2 hits EOF" the abort tests assert into a flaky "step 1 fails
    with connection reset". Draining empties the receive buffer so the
    close delivers a FIN, which preserves already-sent data.

    The drain blocks briefly (bounded by ``_DRAIN_TIMEOUT``) rather than
    polling non-blockingly: in ``pipelining`` mode the client writes all
    requests up front, but some bytes may still be in flight when the
    server reaches this point, so a non-blocking read can miss them and
    leave the RST race open. A short bounded wait lets the in-flight bytes
    arrive. The bound keeps a misbehaving client that never stops writing
    from wedging the server thread.
    """
    conn.settimeout(_DRAIN_TIMEOUT)
    try:
        while True:
            if not conn.recv(4096):
                break
    except (TimeoutError, OSError):
        pass
    finally:
        conn.settimeout(None)


def serve_one(conn: socket.socket) -> None:
    """Read one request, send one response, then close cleanly.

    Order matters for determinism. (1) Read req 1's headers and respond.
    (2) ``shutdown(SHUT_WR)`` flushes the send buffer and sends a FIN, so
    the client receives step 1's response and then a clean EOF on its read
    side for step 2 — exactly the "step 1 = 200, step 2 = connection
    closed" the abort tests assert. (3) Drain any pipelined follow-up
    bytes the client wrote behind req 1 before fully closing: unread bytes
    in the receive buffer at ``close()`` time trigger an RST, which can
    overtake and discard step 1's response already queued in the client's
    kernel receive buffer (the flake that surfaced under parallel load as
    "step 1 failed (connection reset)"). Draining ensures the final close
    is a quiet FIN, not a destructive RST.
    """
    try:
        buf = _read_headers(conn)
        if not buf:
            return
        q = _query(buf)
        body = q.get("body", "step1-ok").encode()
        status = int(q.get("status", "200"))
        _respond(conn, body, status)
        # Half-close the write side: delivers step 1's response + EOF to
        # the client's reader before we touch the read side.
        with contextlib.suppress(OSError):
            conn.shutdown(socket.SHUT_WR)
        _drain_remaining(conn)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            conn.shutdown(socket.SHUT_RDWR)
        conn.close()


def main() -> None:
    """Listen on 127.0.0.1:<port>, serve each connection on its own thread.

    Each connection is handled in a daemon thread so the bounded drain in
    ``serve_one`` (which briefly blocks waiting for in-flight pipelined
    bytes) never stalls the accept loop and starves a reconnecting client
    under load. Daemon threads die with the process.
    """
    port = int(sys.argv[1])
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(16)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=serve_one, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
