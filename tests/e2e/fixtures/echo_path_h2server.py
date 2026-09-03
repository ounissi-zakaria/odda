r"""Raw HTTP/2 server that accepts an invalid ``:path`` (with CRLF) and echoes it.

Reproduces the H2->H1 downgrade gateway scenario where a non-validating
server accepts an H2 ``:path`` containing ``\r\n`` (which hypercorn
rejects via path validation). This server uses the ``h2`` library with
``validate_inbound_headers=False`` so it accepts any ``:path`` value,
then echoes the received ``:path`` verbatim as the response body.

Run as: python3 echo_path_h2server.py <port> <cert.pem> <key.pem>

Used by test 22-request-send-h2-line-terminator.md to prove that a
literal CRLF in the ``:path`` (crafted via a custom line terminator)
survives odda's parse and lands in the H2 frame on the wire.
"""

from __future__ import annotations

import contextlib
import socket
import ssl
import sys

import h2.config
import h2.connection
import h2.events


def main(port: int, cert: str, key: str) -> None:
    """Listen on ``port`` and echo the received ``:path`` as the body."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.set_alpn_protocols(["h2"])
    ctx.load_cert_chain(cert, key)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)
    srv = ctx.wrap_socket(srv, server_side=True)

    while True:
        try:
            conn = srv.accept()[0]
        except OSError:
            continue
        with contextlib.suppress(Exception):
            _serve_one(conn)
        with contextlib.suppress(Exception):
            conn.close()


def _serve_one(conn: socket.socket) -> None:
    config = h2.config.H2Configuration(
        client_side=False,
        header_encoding=False,
        validate_outbound_headers=False,
        normalize_outbound_headers=False,
        validate_inbound_headers=False,
    )
    h2conn = h2.connection.H2Connection(config=config)
    h2conn.initiate_connection()
    conn.sendall(h2conn.data_to_send())

    while True:
        data = conn.recv(65535)
        if not data:
            break
        events = h2conn.receive_data(data)
        conn.sendall(h2conn.data_to_send())
        for event in events:
            if isinstance(event, h2.events.RequestReceived):
                path = b""
                for name, value in event.headers:
                    if name == b":path":
                        path = value
                        break
                body = path
                h2conn.send_headers(
                    stream_id=event.stream_id,
                    headers=[
                        (b":status", b"200"),
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                )
                h2conn.send_data(
                    stream_id=event.stream_id,
                    data=body,
                    end_stream=True,
                )
                conn.sendall(h2conn.data_to_send())


if __name__ == "__main__":
    main(int(sys.argv[1]), sys.argv[2], sys.argv[3])
