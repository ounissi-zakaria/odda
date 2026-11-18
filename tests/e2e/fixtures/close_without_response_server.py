from __future__ import annotations

import contextlib
import socket
import sys


def main() -> None:
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
