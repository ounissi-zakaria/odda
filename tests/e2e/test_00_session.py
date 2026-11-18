from __future__ import annotations

import re

from tests.e2e.conftest import fixture_site, sans_display_urls


async def test_session_boots_and_drives_real_chrome(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        r = await h.call("version", {})
        assert re.fullmatch(r"\d+\.\d+\.\d+", str(r["version"]))

        proxy = await h.call("proxy_url", {})
        assert str(proxy).startswith("http://127.0.0.1:")

        assert await h.call("browser_list", {}) == []

        bid, tid = await h.open_browser(f"{fx.base}/")
        assert tid == 1

        r = await h.call("browser_list", {})
        assert sans_display_urls(r) == [
            {"browser_id": bid, "state": "open", "tab_count": 1}
        ]

        title = await h.eval(bid, tid, "document.title")
        assert title == "Listener Test"

        # See ADR-0027.
        shot = await h.call("screenshot", {"browser_id": bid, "tab_id": tid})
        assert str(shot).endswith(".jpeg")

        await h.call("browser_close", {"browser_id": bid})
        assert await h.call("browser_list", {}) == [
            {"browser_id": bid, "state": "closed"}
        ]


async def test_data_dir_ownership_and_isolation(odda_session, tmp_path) -> None:
    async with odda_session() as h:
        # See ADR-0017.
        assert not h.data_dir.exists()
        assert h.flow_count() == 0
