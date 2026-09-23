"""e2e coverage for the upstream proxy: proxy_upstream_set / clear / get.

The proxy is chained through a local forward-proxy fixture
(``fixtures/forward_proxy.py``). Tests pin the tool contract (state
shape, validation errors for SOCKS/userinfo URLs), that real traffic
(curl absolute-form and a browser CONNECT tunnel) actually traverses
the upstream and is still captured to ``.odda/flows/``, Basic auth
(the upstream's 407 on the absolute-form path is relayed to the
client), and that ``proxy_upstream_clear`` restores direct egress with
no further upstream log entries.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from tests.e2e.conftest import ServerHandle

from tests.e2e.conftest import (
    curl,
    dyn_server,
    fixture_site,
    odda_session,
    script_server,
)


@asynccontextmanager
async def upstream_proxy(
    log_path: Path, require_auth: str | None = None
) -> AsyncIterator[ServerHandle]:
    """Run the forward-proxy fixture; log_path records what it saw."""
    args = ["--log", str(log_path)]
    if require_auth:
        args += ["--require-auth", require_auth]
    async with script_server("forward_proxy.py", *args) as fp:
        yield fp


def log_lines(log_path: Path) -> list[dict]:
    """Parse the fixture's JSONL log (empty when nothing was logged yet)."""
    if not log_path.is_file():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines()]


async def test_upstream_state_initially_direct(odda_session) -> None:
    """A fresh session has no upstream: get reports (None, auth_set=False)."""
    async with odda_session() as h:
        r = await h.call("proxy_upstream_get", {})
        assert r == {"upstream": None, "auth_set": False}


async def test_set_rejects_socks_and_userinfo(odda_session) -> None:
    """socks5:// and schemeless URLs are rejected (http/https only, explicit
    scheme required); userinfo in the URL is rejected with a pointer at
    the auth param; state stays untouched."""
    async with odda_session() as h:
        err = await h.call_error(
            "proxy_upstream_set", {"url": "socks5://127.0.0.1:1080"}
        )
        assert "http" in err and "https" in err
        err = await h.call_error("proxy_upstream_set", {"url": "127.0.0.1:3128"})
        assert "scheme" in err
        err = await h.call_error(
            "proxy_upstream_set", {"url": "http://user:pass@127.0.0.1:3128"}
        )
        assert "auth" in err
        r = await h.call("proxy_upstream_get", {})
        assert r == {"upstream": None, "auth_set": False}


async def test_http_traffic_traverses_upstream(odda_session, tmp_path) -> None:
    """Plain-HTTP curl traffic is forwarded absolute-form through the
    upstream, the response comes back, and the flow is still captured."""
    log_path = tmp_path / "upstream.jsonl"
    async with (
        odda_session() as h,
        fixture_site(index_body="upstream-http-chain") as site,
        upstream_proxy(log_path) as fp,
    ):
        upstream_url = f"http://127.0.0.1:{fp.port}"
        r = await h.call("proxy_upstream_set", {"url": upstream_url})
        assert r["upstream"] == upstream_url
        assert r["auth_set"] is False
        assert "open connections were closed" in r["note"]

        proxy = await h.call("proxy_url", {})
        res = await curl(
            f"{site.base}/?marker=up-chain-http",
            proxy,
            extra=["-w", "%{http_code}"],
        )
        assert res.stdout.endswith("200")
        assert "upstream-http-chain" in res.stdout

        flow = await h.wait_flow("up-chain-http")
        assert (h.data_dir / "flows" / flow["id"]).is_dir()

        entries = log_lines(log_path)
        assert any(
            e["kind"] == "request"
            and f"GET {site.base}/?marker=up-chain-http" in e["line"]
            for e in entries
        ), entries

        assert await h.call("proxy_upstream_get", {}) == {
            "upstream": upstream_url,
            "auth_set": False,
        }


async def test_https_connect_traverses_upstream(odda_session, tmp_path) -> None:
    """A browser CONNECT tunnel opened BEFORE the flip picks up the upstream
    on its next connection; the tunneled https flow is still captured."""
    log_path = tmp_path / "upstream.jsonl"
    async with (
        odda_session() as h,
        dyn_server() as dyn,
        upstream_proxy(log_path) as fp,
    ):
        bid, tid = await h.open_browser()
        r = await h.call("proxy_upstream_set", {"url": f"http://127.0.0.1:{fp.port}"})
        assert r["upstream"] == f"http://127.0.0.1:{fp.port}"

        await h.navigate(bid, tid, f"{dyn.tls_base}/?body=upstream-connect-ok")
        flow = await h.wait_flow("upstream-connect-ok")
        assert (h.data_dir / "flows" / flow["id"]).is_dir()

        connects = [e for e in log_lines(log_path) if e["kind"] == "connect"]
        assert any(f"CONNECT 127.0.0.1:{dyn.port}" in e["line"] for e in connects), (
            connects
        )


async def test_upstream_auth_407_then_ok(odda_session, tmp_path) -> None:
    """Without auth the upstream's 407 is relayed (curl sees 407); after
    setting auth the same chain returns 200."""
    log_path = tmp_path / "upstream.jsonl"
    async with (
        odda_session() as h,
        fixture_site(index_body="auth-chain-body") as site,
        upstream_proxy(log_path, require_auth="me:secret") as fp,
    ):
        await h.call("proxy_upstream_set", {"url": f"http://127.0.0.1:{fp.port}"})
        proxy = await h.call("proxy_url", {})

        res = await curl(
            f"{site.base}/?marker=up-auth-missing",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert res.stdout == "407"
        assert any(
            e["kind"] == "reject" and "up-auth-missing" in e["line"]
            for e in log_lines(log_path)
        )

        r = await h.call(
            "proxy_upstream_set",
            {"url": f"http://127.0.0.1:{fp.port}", "auth": "me:secret"},
        )
        assert r["auth_set"] is True
        res = await curl(
            f"{site.base}/?marker=up-auth-ok",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert res.stdout == "200"
        await h.wait_flow("up-auth-ok")


async def test_clear_restores_direct(odda_session, tmp_path) -> None:
    """After clear, traffic reaches the origin directly — the upstream log
    stays frozen — and get reports the direct state."""
    log_path = tmp_path / "upstream.jsonl"
    async with (
        odda_session() as h,
        fixture_site(index_body="cleared-direct-body") as site,
        upstream_proxy(log_path) as fp,
    ):
        await h.call("proxy_upstream_set", {"url": f"http://127.0.0.1:{fp.port}"})
        proxy = await h.call("proxy_url", {})
        res = await curl(
            f"{site.base}/?marker=up-clear-a",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert res.stdout == "200"
        assert len(log_lines(log_path)) == 1

        r = await h.call("proxy_upstream_clear", {})
        assert r["upstream"] is None
        assert r["auth_set"] is False
        assert "open connections were closed" in r["note"]

        res = await curl(
            f"{site.base}/?marker=up-clear-b",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert res.stdout == "200"
        assert len(log_lines(log_path)) == 1
        assert await h.call("proxy_upstream_get", {}) == {
            "upstream": None,
            "auth_set": False,
        }


async def test_flip_closes_live_pooled_connection(odda_session, tmp_path) -> None:
    """The tester's scenario: a browser with an established (pooled)
    connection keeps the old vantage until the connection dies. The
    flip must close it — deterministic check with a raw client socket
    held open across proxy_upstream_set."""
    async with odda_session() as h:
        proxy = str(await h.call("proxy_url", {}))
        port = int(proxy.rsplit(":", 1)[1])
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.sleep(0.25)  # let mitmproxy register the handler
        r = await h.call("proxy_upstream_set", {"url": "http://127.0.0.1:1"})
        assert r["closed_connections"] >= 1
        data = await asyncio.wait_for(reader.read(), timeout=5)
        assert data == b""  # our socket was closed by the flip
        writer.close()


async def test_flip_applies_to_pooled_browser(odda_session, tmp_path) -> None:
    """End-to-end form of the tester's scenario: navigate (establishing
    Chrome's pooled connection), flip, navigate again — the second
    navigation must traverse the upstream, not the old pooled path."""
    log_path = tmp_path / "upstream.jsonl"
    async with (
        odda_session() as h,
        dyn_server() as dyn,
        upstream_proxy(log_path) as fp,
    ):
        bid, tid = await h.open_browser()
        await h.navigate(bid, tid, f"{dyn.tls_base}/?body=pre-flip-vantage")

        r = await h.call("proxy_upstream_set", {"url": f"http://127.0.0.1:{fp.port}"})
        assert r["closed_connections"] >= 1

        await h.navigate(bid, tid, f"{dyn.tls_base}/?body=post-flip-vantage")
        await h.wait_flow("post-flip-vantage")
        connects = [e for e in log_lines(log_path) if e["kind"] == "connect"]
        assert any(f"CONNECT 127.0.0.1:{dyn.port}" in e["line"] for e in connects), (
            connects
        )


async def test_reset_without_auth_drops_stale_credentials(
    odda_session, tmp_path
) -> None:
    """Re-setting an upstream without auth must clear the old Basic
    credentials — otherwise they leak to the new upstream host while
    auth_set reports false. The auth-requiring fixture answers 407
    without credentials, so a stale credential would show as 200."""
    log_path = tmp_path / "upstream.jsonl"
    async with (
        odda_session() as h,
        fixture_site(index_body="auth-drop-body") as site,
        upstream_proxy(log_path, require_auth="me:secret") as fp,
    ):
        url = f"http://127.0.0.1:{fp.port}"
        await h.call("proxy_upstream_set", {"url": url, "auth": "me:secret"})
        proxy = await h.call("proxy_url", {})
        res = await curl(
            f"{site.base}/?marker=up-auth-drop-ok",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert res.stdout == "200"

        await h.call("proxy_upstream_set", {"url": url})
        assert await h.call("proxy_upstream_get", {}) == {
            "upstream": url,
            "auth_set": False,
        }
        res = await curl(
            f"{site.base}/?marker=up-auth-drop-stale",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert res.stdout == "407"
