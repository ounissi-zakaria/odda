"""Port of scrut 07-request-send-h1.md: request_new creates an empty editable
request + meta.json; a raw HTTP/1.1 request_send records the flow."""

from __future__ import annotations

from tests.e2e.conftest import dyn_server


async def test_request_new_creates_empty_request_and_meta(odda_session) -> None:
    """request_new returns the request's identity (scheme https, host) and
    writes an empty request file plus a meta.json sidecar."""
    async with odda_session() as h, dyn_server() as dyn:
        sc = await h.call(
            "request_new", {"name": "h1-test", "host": "127.0.0.1", "port": dyn.port}
        )
        assert isinstance(sc, dict)
        assert sc["scheme"] == "https"
        assert sc["host"] == "127.0.0.1"

        d = h.data_dir / "requests" / "h1-test"
        assert (d / "request").exists()
        assert (d / "request").stat().st_size == 0
        assert (d / "meta.json").exists()


async def test_send_h1_records_flow(odda_session) -> None:
    """A raw H1 GET send returns the flow record (200, error null, id), stores
    the response body and the on-the-wire request, and records scheme + port
    in flows.jsonl."""
    async with odda_session() as h, dyn_server() as dyn:
        await h.call(
            "request_new", {"name": "h1-test", "host": "127.0.0.1", "port": dyn.port}
        )
        h.write_request(
            "h1-test",
            b"GET /a?body=h1-send-test&status=200&header=Content-Type:application/json"
            b" HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{dyn.port}\r\n".encode()
            + b"Accept: */*\r\n\r\n",
        )

        sc = await h.call_json(
            "request_send", {"name": "h1-test", "insecure": True, "timeout": 10}
        )
        assert isinstance(sc, dict)
        assert sc["status_code"] == 200
        assert sc["error"] is None

        fid = sc["id"]
        body = (h.data_dir / "flows" / fid / "response_body.json").read_bytes()
        assert body == b"h1-send-test"
        stored = (h.data_dir / "flows" / fid / "request").read_bytes()
        assert b"HTTP/1.1" in stored

        rec = h.flow_record(fid)
        assert rec["scheme"] == "https"
        assert rec["port"] == dyn.port
