# See ADR-0013.
from __future__ import annotations

import socket
import ssl
import sys
import urllib.parse

import h2.config
import h2.connection
import h2.events


def main(proxy_url: str, target_host: str, target_port: int, path: str) -> None:
    parsed = urllib.parse.urlparse(proxy_url)
    proxy_host = parsed.hostname or "127.0.0.1"
    proxy_port = parsed.port or 8080

    sock = socket.create_connection((proxy_host, proxy_port), timeout=10)

    connect = (
        f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
        f"Host: {target_host}:{target_port}\r\n\r\n"
    ).encode("ascii")
    sock.sendall(connect)
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            print("h2client=proxy-closed", flush=True)
            sys.exit(1)
        resp += chunk
    first_line = resp.split(b"\r\n", 1)[0]
    if b" 200 " not in first_line:
        print(f"h2client=connect-failed: {first_line!r}", flush=True)
        sys.exit(1)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(["h2"])
    sock = ctx.wrap_socket(sock, server_hostname=target_host)
    if sock.selected_alpn_protocol() != "h2":
        print(f"h2client=no-h2: {sock.selected_alpn_protocol()!r}", flush=True)
        sys.exit(1)

    config = h2.config.H2Configuration(
        client_side=True,
        header_encoding="utf-8",
        validate_outbound_headers=True,
        normalize_outbound_headers=True,
    )
    conn = h2.connection.H2Connection(config=config)
    conn.initiate_connection()
    sock.sendall(conn.data_to_send())

    stream_id = conn.get_next_available_stream_id()
    conn.send_headers(
        stream_id=stream_id,
        headers=[
            (":method", "GET"),
            (":path", path),
            (":scheme", "https"),
            (":authority", f"{target_host}:{target_port}"),
            ("user-agent", "odda-h2-cookie-test"),
            ("cookie", "session=abc"),
            ("cookie", "_lab=val"),
        ],
        end_stream=True,
    )
    sock.sendall(conn.data_to_send())

    stream_ended = False
    while not stream_ended:
        data = sock.recv(65535)
        if not data:
            break
        events = conn.receive_data(data)
        for event in events:
            is_end = isinstance(event, h2.events.StreamEnded)
            if is_end and event.stream_id == stream_id:
                stream_ended = True
        outgoing = conn.data_to_send()
        if outgoing:
            sock.sendall(outgoing)

    print("h2client=ok", flush=True)


if __name__ == "__main__":
    main(
        sys.argv[1],
        sys.argv[2],
        int(sys.argv[3]),
        sys.argv[4],
    )
