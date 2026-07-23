"""Raw HTTP/2 server that emits a header value with leading whitespace.

Reproduces the real-world 502 "HTTP/2 protocol error: Received header
value surrounded by whitespace" observed against hosts like
hackerone.on.symphony.com (which sends ` IE=Edge`). hypercorn strips
header values before sending (hypercorn/utils.py: `build_and_validate_headers`
calls `bytes(value).strip()`), so the standard dyn_asgi.py fixture cannot
reproduce the bug. This server writes raw h2 frames over TLS using the
`h2` library directly, sending `X-Bad-Header: <space>IE=Edge` unsanitized.

Run as: python3 ws_header_h2server.py <port> <cert.pem> <key.pem>
    Then curl/browser through odda's proxy -> https://127.0.0.1:<port>/

Responds 200 with the bad header and body `ws-header-fix-test`.
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
    """Listen on ``port`` and serve one raw h2 response per connection."""
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
                h2conn.send_headers(
                    stream_id=event.stream_id,
                    headers=[
                        (b":status", b"200"),
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", b"18"),
                        # Leading space in the value — invalid per RFC 7540
                        # §8.1.2.5. validate_outbound_headers=False above lets
                        # us send it raw; this is what a non-conformant server
                        # (e.g. hackerone.on.symphony.com) does on the wire.
                        (b"x-bad-header", b" IE=Edge"),
                    ],
                )
                h2conn.send_data(
                    stream_id=event.stream_id,
                    data=b"ws-header-fix-test",
                    end_stream=True,
                )
                conn.sendall(h2conn.data_to_send())


if __name__ == "__main__":
    main(int(sys.argv[1]), sys.argv[2], sys.argv[3])
