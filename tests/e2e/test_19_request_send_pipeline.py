"""e2e port of scrut 19-request-send-pipeline.md: multi-name H1 pipeline.

``request_send`` with ``names=[a, b]`` sends multiple editable requests
on one HTTP/1.1 connection — sequential keep-alive by default,
``pipelining`` for send-all-then-read-all (ADR-0019). Covers the happy
paths, the pre-emptive rejections (fix_content_length, mixed H1+H2,
bare-body), the connect-fail abort (flows.jsonl stays complete), the
mid-sequence close (step 1 200, step 2 descriptive error), the
pipelining close-abort triad (fail + aborted prefix), and the clean
keep-alive regression.
"""

from __future__ import annotations

from tests.e2e.conftest import dyn_server, odda_session, script_server


async def _new(h, name: str, port: int, **kwargs) -> None:
    """Create an editable request pointing at 127.0.0.1:<port>."""
    args = {"name": name, "host": "127.0.0.1", "port": port, "force": True}
    args.update(kwargs)
    await h.call("request_new", args)


async def test_multiname_sequential_two_records(odda_session, tmp_path) -> None:
    """names=[a,b]: list of 2, distinct ids, all 200, bodies per request."""
    async with odda_session() as h, dyn_server() as dyn:
        for n, body in (("pipe-a", "pipe-a-ok"), ("pipe-b", "pipe-b-ok")):
            await _new(h, n, dyn.port)
            h.write_request(
                n,
                (
                    f"GET /a?body={body}&status=200"
                    f"&header=Content-Type:application/json HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{dyn.port}\r\n"
                    f"Connection: keep-alive\r\n\r\n"
                ).encode(),
            )
        recs = await h.call_json(
            "request_send",
            {"names": ["pipe-a", "pipe-b"], "insecure": True, "timeout": 10},
        )
        assert isinstance(recs, list) and len(recs) == 2
        assert recs[0]["id"] != recs[1]["id"]
        assert all(r["status_code"] == 200 for r in recs)
        # bodies come back as the values asked for in each query string
        for rec, body in zip(recs, (b"pipe-a-ok", b"pipe-b-ok"), strict=True):
            f = h.data_dir / "flows" / rec["id"] / "response_body.json"
            assert f.read_bytes() == body


async def test_single_name_stays_a_dict(odda_session, tmp_path) -> None:
    """One name keeps the frozen single-shot contract: dict, not list."""
    async with odda_session() as h, dyn_server() as dyn:
        await _new(h, "pipe-a", dyn.port)
        h.write_request(
            "pipe-a",
            (
                f"GET /a?body=pipe-a-ok&status=200"
                f"&header=Content-Type:application/json HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{dyn.port}\r\n"
                f"Connection: keep-alive\r\n\r\n"
            ).encode(),
        )
        r = await h.call_json(
            "request_send", {"name": "pipe-a", "insecure": True, "timeout": 10}
        )
        assert isinstance(r, dict)


async def test_pipelining_two_records(odda_session, tmp_path) -> None:
    """pipelining=True: write both up front, read both; still 2 records."""
    async with odda_session() as h, dyn_server() as dyn:
        for n in ("pipe-a", "pipe-b"):
            await _new(h, n, dyn.port)
            h.write_request(
                n,
                (
                    f"GET /a?body={n}-ok&status=200"
                    f"&header=Content-Type:application/json HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{dyn.port}\r\n"
                    f"Connection: keep-alive\r\n\r\n"
                ).encode(),
            )
        recs = await h.call_json(
            "request_send",
            {
                "names": ["pipe-a", "pipe-b"],
                "pipelining": True,
                "insecure": True,
                "timeout": 10,
            },
        )
        assert len(recs) == 2
        assert all(r["status_code"] == 200 for r in recs)


async def test_pipeline_rejections(odda_session, tmp_path) -> None:
    """fix_content_length, mixed H1+H2, and bare-body are rejected pre-emptively."""
    async with odda_session() as h, dyn_server() as dyn:
        for n in ("pipe-a", "h2-in-pipe", "bare-body"):
            await _new(h, n, dyn.port)
        h.write_request(
            "pipe-a",
            (
                f"GET /a?body=pipe-a-ok HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{dyn.port}\r\n"
                f"Connection: keep-alive\r\n\r\n"
            ).encode(),
        )
        h.write_request(
            "h2-in-pipe",
            (
                f"GET /a?body=h2-no HTTP/2\r\nHost: 127.0.0.1:{dyn.port}\r\n\r\n"
            ).encode(),
        )
        h.write_request(
            "bare-body",
            (f"POST / HTTP/1.1\r\nHost: 127.0.0.1:{dyn.port}\r\n\r\nhello").encode(),
        )

        err = await h.call_error(
            "request_send",
            {
                "names": ["pipe-a", "pipe-a"],
                "fix_content_length": True,
                "insecure": True,
            },
        )
        assert "smuggling" in err

        err = await h.call_error(
            "request_send",
            {"names": ["pipe-a", "h2-in-pipe"], "insecure": True, "timeout": 10},
        )
        assert "mix" in err

        err = await h.call_error(
            "request_send",
            {"names": ["bare-body", "pipe-a"], "insecure": True, "timeout": 10},
        )
        assert "Content-Length" in err


async def test_connect_fail_aborts_both_records(odda_session, tmp_path) -> None:
    """Port 1 (nothing listens): 2 error records, flows.jsonl grows by 2."""
    async with odda_session() as h, dyn_server() as dyn:
        for n, body in (("pipe-hang-a", "x"), ("pipe-hang-b", "y")):
            await _new(h, n, 1)
            h.write_request(
                n,
                (
                    f"GET /a?body={body} HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{dyn.port}\r\n"
                    f"Connection: keep-alive\r\n\r\n"
                ).encode(),
            )
        before = h.flow_count()
        recs = await h.call_json(
            "request_send",
            {"names": ["pipe-hang-a", "pipe-hang-b"], "insecure": True, "timeout": 3},
        )
        assert len(recs) == 2
        assert all(r["status_code"] is None for r in recs)
        assert all(r.get("error") for r in recs)
        assert h.flow_count() - before == 2


async def test_mid_sequence_close(odda_session, tmp_path) -> None:
    """Step 1 keeps its 200; step 2 (the failed step) carries the cause."""
    async with odda_session() as h, dyn_server() as dyn:
        for n in ("pipe-close-1", "pipe-close-2"):
            await _new(h, n, dyn.port)
        h.write_request(
            "pipe-close-1",
            (
                f"GET /a?body=step1-ok&status=200"
                f"&header=Content-Type:application/json HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{dyn.port}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode(),
        )
        h.write_request(
            "pipe-close-2",
            (
                f"GET /a?body=step2 HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{dyn.port}\r\n"
                f"Connection: keep-alive\r\n\r\n"
            ).encode(),
        )
        recs = await h.call_json(
            "request_send",
            {
                "names": ["pipe-close-1", "pipe-close-2"],
                "insecure": True,
                "timeout": 10,
            },
        )
        assert len(recs) == 2
        assert recs[0]["status_code"] == 200
        assert recs[0]["error"] is None
        assert recs[1]["status_code"] is None
        assert isinstance(recs[1]["error"], str) and len(recs[1]["error"]) > 0
        # the failed step's error names the cause, not the abort prefix
        assert "connection closed before response headers" in recs[1]["error"]
        # step 1's response body was preserved (read before the close)
        body = (
            h.data_dir / "flows" / recs[0]["id"] / "response_body.json"
        ).read_bytes()
        assert body == b"step1-ok"


async def test_pipelining_close_abort_triad(odda_session, tmp_path) -> None:
    """pipelining 3 to close-after-first: 200, cause, aborted:step-2 prefix."""
    async with odda_session() as h, script_server("close_after_first_server.py") as caf:
        for n in ("pipe-abort-1", "pipe-abort-2", "pipe-abort-3"):
            await _new(h, n, caf.port, protocol="http")
        h.write_request(
            "pipe-abort-1",
            b"GET /a?body=pa1-ok&status=200 HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n",
        )
        for n, path in (("pipe-abort-2", "/b"), ("pipe-abort-3", "/c")):
            h.write_request(
                n,
                (
                    f"GET {path} HTTP/1.1\r\n"
                    f"Host: 127.0.0.1\r\n"
                    f"Connection: keep-alive\r\n\r\n"
                ).encode(),
            )
        recs = await h.call_json(
            "request_send",
            {
                "names": ["pipe-abort-1", "pipe-abort-2", "pipe-abort-3"],
                "pipelining": True,
                "timeout": 10,
            },
        )
        assert len(recs) == 3
        assert recs[0]["status_code"] == 200
        assert recs[0]["error"] is None
        assert recs[1]["status_code"] is None
        assert "connection closed before response headers" in recs[1]["error"]
        assert recs[2]["status_code"] is None
        assert recs[2]["error"].startswith("aborted: step 2 failed")


async def test_pipelining_clean_keepalive_regression(odda_session, tmp_path) -> None:
    """Clean 3-request pipelining to keep-alive: all 200, no false aborts."""
    async with odda_session() as h, dyn_server() as dyn:
        for n in ("pipe-clean-1", "pipe-clean-2", "pipe-clean-3"):
            await _new(h, n, dyn.port)
            h.write_request(
                n,
                (
                    f"GET /a?body={n.replace('pipe-clean', 'pc')}-ok&status=200"
                    f"&header=Content-Type:application/json HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{dyn.port}\r\n"
                    f"Connection: keep-alive\r\n\r\n"
                ).encode(),
            )
        recs = await h.call_json(
            "request_send",
            {
                "names": ["pipe-clean-1", "pipe-clean-2", "pipe-clean-3"],
                "pipelining": True,
                "insecure": True,
                "timeout": 10,
            },
        )
        assert len(recs) == 3
        assert all(r["status_code"] == 200 for r in recs)
        assert all(r["error"] is None for r in recs)
