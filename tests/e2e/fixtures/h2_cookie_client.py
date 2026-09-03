"""Raw HTTP/2 client that sends split ``Cookie`` fragments through a proxy.

Reproduces the capture-side cookie-join defect fixed by ADR-0013. An
HTTP/2 client is permitted by RFC 7540 §8.1.2.5 to split cookie pairs
across multiple ``cookie`` header fields. Chrome pre-joins with ``"; "``;
this client deliberately does not, sending two ``cookie`` fragments so
mitmproxy surfaces them as separate headers and odda's
``_build_request_bytes`` is exercised on the join path.

curl cannot reproduce this — `curl --http2` joins cookies before HPACK
encoding, mirroring Chrome. So this client writes raw h2 frames itself
via the `h2` library (already a runtime dep of odda/mitmproxy).

Usage:
    h2_cookie_client.py <proxy_url> <target_host> <target_port> <path>

<proxy_url> is the value from the `proxy_url` tool (e.g. ``http://127.0.0.1:PORT``),
parsed with urllib so the shell need not split host/port.

CONNECTs through the HTTP proxy, then opens an h2 connection to
<target_host>:<target_port> (TLS, ALPN h2, cert verification disabled),
and sends a single GET with two ``cookie`` fragments: ``session=abc`` and
``_lab=val``. Prints ``h2client=ok`` on success so the scrut doc can
assert it.

Exits 0 on success, non-zero on any error.
"""

from __future__ import annotations

import socket
import ssl
import sys
import urllib.parse

import h2.config
import h2.connection
import h2.events


def main(proxy_url: str, target_host: str, target_port: int, path: str) -> None:
    """CONNECT through the proxy, open an h2 stream, send split cookie fragments.

    Prints ``h2client=ok`` on success; exits non-zero on any error.
    """
    parsed = urllib.parse.urlparse(proxy_url)
    proxy_host = parsed.hostname or "127.0.0.1"
    proxy_port = parsed.port or 8080

    # 1. Open a plain TCP connection to the HTTP proxy.
    sock = socket.create_connection((proxy_host, proxy_port), timeout=10)

    # 2. Issue a CONNECT tunnel to the target. The proxy (mitmproxy via
    #    odda) establishes a TLS connection upstream and bridges bytes
    #    through the tunnel.
    connect = (
        f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
        f"Host: {target_host}:{target_port}\r\n\r\n"
    ).encode("ascii")
    sock.sendall(connect)
    # Read the 200 Connection Established response.
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

    # 3. Wrap the tunnel in TLS with ALPN h2, talking to the target
    #    (mitmproxy re-encrypts upstream; from this socket's view the
    #    peer is the proxy's upstream-side TLS).
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(["h2"])
    sock = ctx.wrap_socket(sock, server_hostname=target_host)
    if sock.selected_alpn_protocol() != "h2":
        print(f"h2client=no-h2: {sock.selected_alpn_protocol()!r}", flush=True)
        sys.exit(1)

    # 4. Send an h2 GET with TWO cookie fragments. RFC 7540 §8.1.2.5
    #    permits this; RFC 6265 §5.4 says a UA SHOULD join with "; ", but
    #    the wire format allows the split. We deliberately split to
    #    exercise odda's capture-side join.
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
            # Two cookie fragments — the crux of the test.
            ("cookie", "session=abc"),
            ("cookie", "_lab=val"),
        ],
        end_stream=True,
    )
    sock.sendall(conn.data_to_send())

    # 5. Read the response; we only care that the exchange completes so
    #    mitmproxy flushes the captured flow to disk. The scrut doc
    #    asserts on the captured `request` file, not on this response.
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
