from __future__ import annotations

import contextlib
import socket
import sys
import threading
import urllib.parse

_HEADER_TERMINATOR = b"\r\n\r\n"
_MIN_REQUEST_LINE_PARTS = 2
_DRAIN_TIMEOUT = 0.05


def _read_headers(sock: socket.socket) -> bytes:
    buf = b""
    while _HEADER_TERMINATOR not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def _query(buf: bytes) -> dict[str, str]:
    first_line = buf.split(b"\r\n", 1)[0]
    parts = first_line.split(b" ")
    path = parts[1] if len(parts) >= _MIN_REQUEST_LINE_PARTS else b"/"
    raw_qs = path.split(b"?", 1)[1] if b"?" in path else b""
    return {k: v[0] for k, v in urllib.parse.parse_qs(raw_qs).items()}


def _respond(sock: socket.socket, body: bytes, status: int) -> None:
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
    try:
        buf = _read_headers(conn)
        if not buf:
            return
        q = _query(buf)
        body = q.get("body", "step1-ok").encode()
        status = int(q.get("status", "200"))
        _respond(conn, body, status)
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
