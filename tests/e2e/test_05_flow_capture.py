"""Port of scrut 05-flow-capture.md: proxy and flow capture.

A captured HTTP flow through the odda proxy lands in
``<data_dir>/flows/flows.jsonl`` plus a per-flow directory with
``request``, ``response_headers``, ``response_body.<ext>``, all
read-only (mode 0444). No browser is needed — a curl through
proxy_url with a unique ``?marker=`` is matched via wait_flow, since
Chrome background traffic also flows through the proxy.
"""

import stat

from tests.e2e.conftest import curl, fixture_site


async def test_captured_flow_lands_in_flowstore(odda_session) -> None:
    """curl through proxy_url is captured to flows.jsonl plus a per-flow
    dir (request / response_headers / response_body.html), all mode 0444."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        proxy = await h.call("proxy_url", {})
        assert str(proxy).startswith("http://127.0.0.1:")

        marker = "flow-capture-test"
        r = await curl(
            f"{fx.base}/?marker={marker}",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"

        flow = await h.wait_flow(marker)

        # flows.jsonl is created and non-empty, and the matched record
        # is for our localhost request.
        flows_file = h.data_dir / "flows" / "flows.jsonl"
        assert flows_file.is_file()
        assert h.flow_count() >= 1

        # The per-flow directory exists with the three artifacts.
        flow_dir = h.data_dir / "flows" / flow["id"]
        assert flow_dir.is_dir()
        request = flow_dir / "request"
        response_headers = flow_dir / "response_headers"
        response_body = flow_dir / "response_body.html"
        assert request.is_file()
        assert response_headers.is_file()
        assert response_body.is_file()

        # Per-flow files are read-only (mode 0444).
        for f in (request, response_headers, response_body):
            assert stat.S_IMODE(f.stat().st_mode) == 0o444, f"{f.name} mode"
