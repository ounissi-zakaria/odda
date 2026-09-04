"""Proxy accepts HTTP/2 responses with whitespace-padded header values (scrut 15).

Real-world servers emit header values with leading/trailing whitespace;
strict h2 validation would surface that as a 502, blocking capture of
the very hosts odda targets. The proxy skips inbound header validation,
so both curl and a real browser navigation through it complete with
status 200 and no flow error. Upstream is a raw-socket h2 server that
sends `X-Bad-Header: <space>IE=Edge` unsanitized.
"""

from __future__ import annotations

from tests.e2e.conftest import curl, script_server, self_signed_cert


async def test_ws_padded_h2_header_does_not_502(odda_session, tmp_path) -> None:
    """curl and browser navigations through the proxy to the bad-header
    server both complete with status_code 200 and error None."""
    key, cert = self_signed_cert(tmp_path)

    async with (
        odda_session() as h,
        script_server("ws_header_h2server.py", str(cert), str(key)) as srv,
    ):
        proxy = await h.call("proxy_url", {})

        # Request side: curl --http2 tunnels through the proxy, mitmproxy
        # re-establishes h2 upstream, the server answers with the padded
        # header — 200, not the pre-fix 502.
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

        # Browser side: the reported symptom was a 502 when navigating
        # via odda's browser — same proxy path, real navigate tool.
        bid, tid = await h.open_browser()
        nav = await h.navigate(bid, tid, f"{srv.tls_base}/?marker=ws-header-browser")
        assert nav["status"].startswith("Navigated to: ")

        rec = await h.wait_flow("ws-header-browser")
        assert rec["status_code"] == 200
        assert rec["error"] is None

        await h.call("browser_close", {"browser_id": bid})
