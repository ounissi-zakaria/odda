from __future__ import annotations

from tests.e2e.conftest import curl, script_server, self_signed_cert


async def test_ws_padded_h2_header_does_not_502(odda_session, tmp_path) -> None:
    key, cert = self_signed_cert(tmp_path)

    async with (
        odda_session() as h,
        script_server("ws_header_h2server.py", str(cert), str(key)) as srv,
    ):
        proxy = await h.call("proxy_url", {})

        r = await curl(
            f"{srv.tls_base}/?marker=ws-header-curl",
            proxy,
            extra=["--http2", "-o", "/dev/null", "-w", "curl_status=%{http_code}"],
        )
        assert r.stdout.strip().endswith("curl_status=200"), (
            f"curl through proxy failed: {r.stdout!r} {r.stderr!r}"
        )

        rec = await h.wait_flow("ws-header-curl")
        assert rec["status_code"] == 200
        assert rec["error"] is None

        bid, tid = await h.open_browser()
        nav = await h.navigate(bid, tid, f"{srv.tls_base}/?marker=ws-header-browser")
        assert nav["status"].startswith("Navigated to: ")

        rec = await h.wait_flow("ws-header-browser")
        assert rec["status_code"] == 200
        assert rec["error"] is None

        await h.call("browser_close", {"browser_id": bid})
