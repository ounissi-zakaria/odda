# See ADR-0030.
from __future__ import annotations

import os
import re
from pathlib import Path

from mcp import Client

from tests.e2e.conftest import Harness, curl, dyn_server, fixture_site


async def test_two_browsers_attribute_to_own_tokens(odda_session) -> None:
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
    async with odda_session() as h, dyn_server() as dyn:
        bid, tid = await h.open_browser()
        await h.navigate(bid, tid, f"{dyn.tls_base}/?marker=https-attr")
        flow = await h.wait_flow("https-attr")
        assert flow["browser_id"] == bid


async def test_non_browser_traffic_attributes_to_null(odda_session) -> None:
    async with odda_session() as h, fixture_site(index_body="hi") as fx:
        proxy = await h.call("proxy_url", {})
        assert str(proxy).startswith("http://127.0.0.1:")

        r = await curl(f"{fx.base}/?marker=curl-only", proxy)
        assert "curl-only" in r.stdout or r.returncode == 0

        flow = await h.wait_flow("curl-only")
        assert flow["browser_id"] is None


async def test_canary_exchange_is_never_recorded(odda_session) -> None:
    async with odda_session() as h:
        await h.open_browser()
        assert h.flows_with("odda-seed.invalid") == []


async def test_tokens_are_project_lifetime_and_dirs_inert(
    odda_session, tmp_path: Path, config_home: Path
) -> None:
    # See ADR-0030.
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
            h2 = Harness(client, data_dir, config_home)
            t2, _ = await h2.open_browser()
            assert t2 != t1

            fresh = await h2.call("userscript_list", {"browser_id": t2})
            assert all(s["name"] != "session-one" for s in fresh)

            old = await h2.call("userscript_list", {"browser_id": t1})
            assert [s["name"] for s in old] == ["session-one"]
            assert (
                data_dir / "browsers" / t1 / "userscripts" / "session-one" / "script.js"
            ).is_file()
    finally:
        os.chdir(old_cwd)
