"""Port of scrut 08-request-send-extra.md: request_send HTTP/2 negotiation,
fix_content_length CRLF preservation (H1 + H2 paths), gzip decode, TLS and
connection-close error flows, empty-file rejection, excluded body storage."""

from __future__ import annotations

import json

from tests.e2e.conftest import dyn_server, script_server


async def test_h2_negotiated_when_request_line_says_http2(odda_session) -> None:
    """An ``HTTP/2`` request line makes the send negotiate h2 via ALPN: 200,
    decoded body, and stored response headers showing HTTP/2."""
    async with odda_session() as h, dyn_server() as dyn:
        await h.call(
            "request_new", {"name": "h2-test", "host": "127.0.0.1", "port": dyn.port}
        )
        h.write_request(
            "h2-test",
            b"GET /a?body=h2-send-test&status=200&header=Content-Type:application/json"
            b" HTTP/2\r\nuser-agent: odda-test\r\naccept: */*\r\n\r\n",
        )

        sc = await h.call_json(
            "request_send", {"name": "h2-test", "insecure": True, "timeout": 10}
        )
        assert sc["status_code"] == 200

        fid = sc["id"]
        body = (h.data_dir / "flows" / fid / "response_body.json").read_bytes()
        assert body == b"h2-send-test"
        hdrs = (h.data_dir / "flows" / fid / "response_headers").read_bytes()
        assert b"HTTP/2" in hdrs


async def test_fix_content_length_preserves_crlf_after_cl(odda_session) -> None:
    """fix_content_length rewrites the declared CL to the real body size without
    eating the CR when another header follows (H1 path); the editable file
    keeps the declared 999."""
    async with odda_session() as h, dyn_server() as dyn:
        await h.call(
            "request_new", {"name": "cl-crlf", "host": "127.0.0.1", "port": dyn.port}
        )
        h.write_request(
            "cl-crlf",
            b"POST /a?body=cl-crlf-ok&status=200&header=Content-Type:application/json"
            b" HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{dyn.port}\r\n".encode()
            + b"Content-Type: application/json\r\nContent-Length: 999\r\n"
            b"X-Order: trailing\r\nConnection: close\r\n\r\npostbody",
        )

        sc = await h.call_json(
            "request_send",
            {
                "name": "cl-crlf",
                "fix_content_length": True,
                "insecure": True,
                "timeout": 10,
            },
        )
        assert sc["status_code"] == 200

        wire = (h.data_dir / "flows" / sc["id"] / "request").read_bytes()
        want = b"Content-Length: 8\r\nX-Order: trailing\r\n"
        i = wire.find(b"Content-Length")
        assert want in wire, repr(wire[i : i + 40])

        editable = (h.data_dir / "requests" / "cl-crlf" / "request").read_bytes()
        assert b"Content-Length: 999" in editable


async def test_fix_content_length_preserves_crlf_on_h2_path(odda_session) -> None:
    """The CL rewrite runs on the raw bytes before the H2/H1 branch, so an
    HTTP/2 request with CL followed by a trailing header keeps CRLF too."""
    async with odda_session() as h, dyn_server() as dyn:
        await h.call(
            "request_new", {"name": "cl-h2", "host": "127.0.0.1", "port": dyn.port}
        )
        h.write_request(
            "cl-h2",
            b"POST /a?body=cl-h2-ok&status=200&header=Content-Type:application/json"
            b" HTTP/2\r\n"
            + f"Host: 127.0.0.1:{dyn.port}\r\n".encode()
            + b"Content-Type: application/json\r\nContent-Length: 999\r\n"
            b"X-Order: trailing\r\n\r\nh2body",
        )

        sc = await h.call_json(
            "request_send",
            {
                "name": "cl-h2",
                "fix_content_length": True,
                "insecure": True,
                "timeout": 10,
            },
        )
        assert sc["status_code"] == 200

        wire = (h.data_dir / "flows" / sc["id"] / "request").read_bytes()
        want = b"Content-Length: 6\r\nX-Order: trailing\r\n"
        i = wire.find(b"Content-Length")
        assert want in wire, repr(wire[i : i + 40])


async def test_gzip_response_is_decoded(odda_session) -> None:
    """A gzip=1 dyn response is decoded: the stored response_body.json holds
    the plain body, not the compressed bytes."""
    async with odda_session() as h, dyn_server() as dyn:
        await h.call(
            "request_new", {"name": "gzip-test", "host": "127.0.0.1", "port": dyn.port}
        )
        h.write_request(
            "gzip-test",
            b"GET /a?body=gzip-decoded-ok&status=200&"
            b"header=Content-Type:application/json&gzip=1 HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{dyn.port}\r\n".encode()
            + b"Connection: close\r\n\r\n",
        )

        sc = await h.call_json(
            "request_send", {"name": "gzip-test", "insecure": True, "timeout": 10}
        )
        assert sc["status_code"] == 200
        body = (h.data_dir / "flows" / sc["id"] / "response_body.json").read_bytes()
        assert body == b"gzip-decoded-ok"


async def test_tls_failure_is_error_flow_not_tool_error(odda_session) -> None:
    """Without insecure, the self-signed TLS failure is captured as an error
    flow (status_code null + error message, not a tool error); insecure then
    succeeds."""
    async with odda_session() as h, dyn_server() as dyn:
        await h.call(
            "request_new",
            {"name": "insecure-test", "host": "127.0.0.1", "port": dyn.port},
        )
        h.write_request(
            "insecure-test",
            b"GET / HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{dyn.port}\r\n".encode()
            + b"Connection: close\r\n\r\n",
        )

        # Inspect the raw CallToolResult: the failure must NOT be a tool
        # error, and request_send is text-first (ADR 0023) — the record
        # arrives as the single JSON text block, no structured channel.
        r = await h.client.call_tool(
            "request_send", {"name": "insecure-test", "timeout": 5}
        )
        assert r.is_error is False
        assert r.structured_content is None
        sc = json.loads(r.content[0].text)
        assert sc["status_code"] is None
        err = sc.get("error")
        assert isinstance(err, str) and err

        sc = await h.call_json(
            "request_send", {"name": "insecure-test", "insecure": True, "timeout": 5}
        )
        assert sc["status_code"] == 200


async def test_connection_close_without_response_is_error_flow(odda_session) -> None:
    """A server that accepts then closes without response headers yields an
    error flow naming the close — not the status_code 0 pseudo-response."""
    async with (
        odda_session() as h,
        script_server("close_without_response_server.py") as srv,
    ):
        await h.call(
            "request_new",
            {
                "name": "close-no-resp",
                "host": "127.0.0.1",
                "port": srv.port,
                "protocol": "http",
            },
        )
        h.write_request(
            "close-no-resp",
            b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n",
        )

        sc = await h.call_json("request_send", {"name": "close-no-resp", "timeout": 5})
        assert sc["status_code"] is None
        assert "connection closed" in (sc.get("error") or "")


async def test_empty_request_file_is_rejected(odda_session) -> None:
    """request_send against the empty file request_new created is a tool error
    naming the empty file."""
    async with odda_session() as h:
        await h.call("request_new", {"name": "empty-test", "host": "127.0.0.1"})
        err = await h.call_error("request_send", {"name": "empty-test", "timeout": 5})
        assert "Request file is empty" in err


async def test_excluded_content_type_body_is_stored(odda_session) -> None:
    """request_send stores the response body even for excluded Content-Types
    (image/jpeg) that browser capture would drop."""
    async with odda_session() as h, dyn_server() as dyn:
        await h.call(
            "request_new", {"name": "img-test", "host": "127.0.0.1", "port": dyn.port}
        )
        h.write_request(
            "img-test",
            b"GET /a?body=secret-file-contents&status=200&header=Content-Type:image/jpeg"
            b" HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{dyn.port}\r\n".encode()
            + b"Connection: close\r\n\r\n",
        )

        sc = await h.call_json(
            "request_send", {"name": "img-test", "insecure": True, "timeout": 10}
        )
        assert sc["status_code"] == 200
        assert sc["body_file"].endswith("response_body.jpeg")

        body = (h.data_dir / "flows" / sc["id"] / "response_body.jpeg").read_bytes()
        assert body == b"secret-file-contents"
