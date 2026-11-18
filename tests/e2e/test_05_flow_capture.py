import stat

from tests.e2e.conftest import curl, fixture_site


async def test_captured_flow_lands_in_flowstore(odda_session) -> None:
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

        flows_file = h.data_dir / "flows" / "flows.jsonl"
        assert flows_file.is_file()
        assert h.flow_count() >= 1

        flow_dir = h.data_dir / "flows" / flow["id"]
        assert flow_dir.is_dir()
        request = flow_dir / "request"
        response_headers = flow_dir / "response_headers"
        response_body = flow_dir / "response_body.html"
        assert request.is_file()
        assert response_headers.is_file()
        assert response_body.is_file()

        for f in (request, response_headers, response_body):
            assert stat.S_IMODE(f.stat().st_mode) == 0o444, f"{f.name} mode"
