"""Scanner tracer bullet: the first Finding lands end-to-end.

A session-live passive scanner observes every flow the session writes
— the flow record writer's post-append hook enqueues, an asyncio drain
task owned by the lifespan scans — and appends findings to
``<data_dir>/scanner/findings.jsonl`` (one line per rule–flow pair,
evidence aggregated). One builtin rule exists:
``disclosure.stack-trace`` (HIGH). External behavior only: traffic in
via curl through proxy_url or request_send, findings out via a bounded
poll of the index (the ``wait_flow`` precedent).
"""

from tests.e2e.conftest import curl, fixture_site

_FX = "stack-trace.html"


async def test_stack_trace_response_lands_finding(odda_session) -> None:
    """A stack-trace body driven through the proxy lands one Finding with
    rule id, flow id, severity high, title, located evidence, and refs."""
    async with odda_session() as h, fixture_site([_FX]) as fx:
        proxy = await h.call("proxy_url", {})
        marker = "scan-tracer"
        r = await curl(
            f"{fx.base}/{_FX}?marker={marker}",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"

        flow = await h.wait_flow(marker)
        finding = await h.wait_finding(flow["id"])

        assert finding["rule_id"] == "disclosure.stack-trace"
        assert finding["flow_id"] == flow["id"]
        assert finding["severity"] == "high"
        assert finding["title"]
        assert "CWE-209" in finding["refs"]
        first = finding["evidence"][0]
        assert first["where"] == "response_body"
        assert isinstance(first["offset"], int)
        assert "Traceback" in first["detail"]


async def test_multi_match_aggregates_into_one_finding(odda_session) -> None:
    """Multiple trace matches inside one flow aggregate into that single
    Finding's evidence — one Finding per rule–flow pair."""
    async with odda_session() as h, fixture_site([_FX]) as fx:
        proxy = await h.call("proxy_url", {})
        marker = "scan-aggregate"
        r = await curl(
            f"{fx.base}/{_FX}?marker={marker}",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"

        flow = await h.wait_flow(marker)
        finding = await h.wait_finding(flow["id"])

        # The fixture's match inventory: one Python traceback, two Java
        # frames, a PHP fatal plus two PHP frames, two Node frames, one
        # .NET frame — nine matches, one finding.
        assert len(finding["evidence"]) == 9
        assert len({e["offset"] for e in finding["evidence"]}) == 9
        assert len(h.findings_for(flow["id"])) == 1


async def test_request_send_flow_is_scanned(odda_session) -> None:
    """The observation hook covers the request sender's write path too —
    request_send records flow on the event-loop thread, browser capture
    on the mitmproxy addon thread."""
    async with odda_session() as h, fixture_site([_FX]) as fx:
        await h.call(
            "request_new",
            {
                "name": "scan-reqsend",
                "host": "127.0.0.1",
                "protocol": "http",
                "port": fx.port,
            },
        )
        h.write_request(
            "scan-reqsend",
            (
                f"GET {fx.path}/{_FX} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{fx.port}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode(),
        )
        rec = await h.call_json(
            "request_send", {"name": "scan-reqsend", "timeout": 10}
        )
        assert isinstance(rec, dict)

        finding = await h.wait_finding(rec["id"])
        assert finding["rule_id"] == "disclosure.stack-trace"
        assert finding["flow_id"] == rec["id"]
