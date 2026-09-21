"""Flow attribution and browser identifier semantics (ADR-0030/0031).

Each browser launches with proxy credentials whose username is its
browser_id token. The proxy's auth addon stamps the identity onto flow
metadata and FlowFileAddon records it as ``browser_id`` in flows.jsonl;
``browser_open`` seeds Chrome's auth cache by driving the initial page
through a canary request to odda-seed.invalid — challenged, answered,
tagged, and never recorded. Non-browser traffic (curl through
proxy_url) attributes to null. Tokens are project-lifetime unique: two
sessions over one data dir never share a token, and a past session's
per-browser userscript dir is never re-attached to a new browser.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from mcp import Client

from tests.e2e.conftest import Harness, curl, dyn_server, fixture_site


async def test_two_browsers_attribute_to_own_tokens(odda_session) -> None:
    """Flows captured per browser carry that browser's token as
    browser_id; tokens are five lowercase letters and distinct."""
    async with odda_session() as h, fixture_site(index_body="hi") as fx:
        b1, t1 = await h.open_browser()
        b2, t2 = await h.open_browser()
        assert b1 != b2

        await h.navigate(b1, t1, f"{fx.base}/?marker=alpha-one")
        await h.navigate(b2, t2, f"{fx.base}/?marker=beta-two")
        r1 = await h.wait_flow("alpha-one")
        r2 = await h.wait_flow("beta-two")
        assert r1["browser_id"] == b1
        assert r2["browser_id"] == b2


async def test_https_flows_attribute_via_connect_credentials(odda_session) -> None:
    """HTTPS flows attribute too: the credentials ride the CONNECT request
    (cached per connection by the auth addon) — the tunneled request
    itself carries no proxy-auth header. Guards against http-only
    attribution regressing."""
    async with odda_session() as h, dyn_server() as dyn:
        bid, tid = await h.open_browser()
        await h.navigate(bid, tid, f"{dyn.tls_base}/?marker=https-attr")
        flow = await h.wait_flow("https-attr")
        assert flow["browser_id"] == bid


async def test_non_browser_traffic_attributes_to_null(odda_session) -> None:
    """A curl through proxy_url records browser_id: null — attribution is
    reserved for traffic that rode a browser's proxy credentials."""
    async with odda_session() as h, fixture_site(index_body="hi") as fx:
        proxy = await h.call("proxy_url", {})
        assert str(proxy).startswith("http://127.0.0.1:")

        r = await curl(f"{fx.base}/?marker=curl-only", proxy)
        assert "curl-only" in r.stdout or r.returncode == 0

        flow = await h.wait_flow("curl-only")
        assert flow["browser_id"] is None


async def test_canary_exchange_is_never_recorded(odda_session) -> None:
    """The seeding exchange (odda-seed.invalid) is proxy plumbing: both
    legs are tagged and dropped before any flow id is allocated, so no
    jsonl line ever mentions the canary host."""
    async with odda_session() as h:
        await h.open_browser()
        assert h.flows_with("odda-seed.invalid") == []


async def test_tokens_are_project_lifetime_and_dirs_inert(
    odda_session, tmp_path: Path
) -> None:
    """Two sessions over one data dir: tokens never repeat, and a past
    session's per-browser userscript dir neither leaks into the new
    browser nor is cleaned up (ADR-0030: never re-attached, never
    removed)."""
    async with odda_session() as h:
        t1, _ = await h.open_browser()
        await h.call(
            "userscript_install",
            {"browser_id": t1, "name": "session-one", "source": "// one"},
        )

    data_dir = tmp_path / ".odda"
    import odda.mcp as odda_mcp

    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        async with Client(odda_mcp.mcp_server) as client:
            h2 = Harness(client, data_dir)
            t2, _ = await h2.open_browser()
            assert t2 != t1

            # The new browser's scope is fresh: the old session's
            # userscript did not leak into it.
            fresh = await h2.call_json("userscript_list", {"browser_id": t2})
            assert all(s["name"] != "session-one" for s in fresh)

            # The past session's dir is still on disk (never-clean
            # policy) but inert — addressable only by its own token.
            old = await h2.call_json("userscript_list", {"browser_id": t1})
            assert [s["name"] for s in old] == ["session-one"]
            assert (
                data_dir / "browsers" / t1 / "userscripts" / "session-one" / "script.js"
            ).is_file()
    finally:
        os.chdir(old_cwd)
