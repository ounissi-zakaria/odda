"""e2e port of scrut 22-request-send-h2-line-terminator.md.

H2 request files are frame-source: odda parses the file into header
values and builds H2 frames, splitting header lines on the line
terminator (default ``\\r\\n``). For H2→H1 downgrade smuggling a literal
CRLF must live inside ``:path``; a custom terminator (``\\x00``) makes
the parser split on null bytes so the CRLF survives into the frame
(ADR-0021). H1 request files are wire-faithful — the terminator is
ignored for them.
"""

from __future__ import annotations

import json

from tests.e2e.conftest import dyn_server, odda_session, script_server, self_signed_cert


async def test_default_terminator_crlf_in_path_rejected(odda_session, tmp_path) -> None:
    """Default \\r\\n terminator: CRLF in :path splits the parse → tool error."""
    key, cert = self_signed_cert(tmp_path)
    async with (
        odda_session() as h,
        script_server("echo_path_h2server.py", str(cert), str(key)) as echo,
    ):
        await h.call(
            "request_new",
            {
                "name": "smuggle-default",
                "host": "127.0.0.1",
                "port": echo.port,
                "force": True,
            },
        )
        h.write_request(
            "smuggle-default",
            (
                f"GET /foo\r\nX-Evil:yes HTTP/2\r\nHost: 127.0.0.1:{echo.port}\r\n\r\n"
            ).encode(),
        )
        err = await h.call_error(
            "request_send", {"name": "smuggle-default", "insecure": True, "timeout": 10}
        )
        assert "Invalid request line" in err


async def test_custom_terminator_preserves_crlf_into_h2_frame(
    odda_session, tmp_path
) -> None:
    """line_terminator=[0]: \\x00 splits lines, CRLF survives into :path."""
    key, cert = self_signed_cert(tmp_path)
    async with (
        odda_session() as h,
        script_server("echo_path_h2server.py", str(cert), str(key)) as echo,
    ):
        await h.call(
            "request_new",
            {
                "name": "h2smuggle",
                "host": "127.0.0.1",
                "port": echo.port,
                "line_terminator": [0],
                "force": True,
            },
        )
        # meta.json carries the terminator as a list of byte ints
        meta = json.loads(
            (h.data_dir / "requests" / "h2smuggle" / "meta.json").read_text()
        )
        assert meta["line_terminator"] == [0]

        # \x00 as line terminator; the :path contains \r\nX-Evil:yes
        h.write_request(
            "h2smuggle",
            (
                f"GET /foo\r\nX-Evil:yes HTTP/2\x00Host: 127.0.0.1:{echo.port}\x00\x00"
            ).encode(),
        )
        rec = await h.call_json(
            "request_send", {"name": "h2smuggle", "insecure": True, "timeout": 10}
        )
        assert rec["status_code"] == 200
        # the structured record's path carries the CRLF verbatim
        assert rec["path"] == "/foo\r\nX-Evil:yes"
        # the echo server returned the :path as the body — CRLF on the wire
        body = (h.data_dir / "flows" / rec["id"] / "response_body.txt").read_bytes()
        assert b"/foo\r\nX-Evil:yes" in body


async def test_h1_ignores_custom_terminator(odda_session, tmp_path) -> None:
    """An H1 request line ignores the \\x00 terminator in meta.json."""
    async with odda_session() as h, dyn_server() as dyn:
        await h.call(
            "request_new",
            {
                "name": "h1-lt",
                "host": "127.0.0.1",
                "port": dyn.port,
                "line_terminator": [0],
                "force": True,
            },
        )
        h.write_request(
            "h1-lt",
            (
                f"GET /a?body=h1-lt-ok&status=200 HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{dyn.port}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode(),
        )
        rec = await h.call_json(
            "request_send", {"name": "h1-lt", "insecure": True, "timeout": 10}
        )
        assert rec["status_code"] == 200
        body = (h.data_dir / "flows" / rec["id"] / "response_body.txt").read_bytes()
        assert body == b"h1-lt-ok"
