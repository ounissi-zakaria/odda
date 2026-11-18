# See ADR-0005.
from __future__ import annotations

from tests.e2e.conftest import fixture_site


def _fixture_counts(r: dict) -> list[int]:
    return [
        x["count"]
        for s in r["scripts"]
        if s.get("url") and "coverage.html" in s["url"]
        for f in s["functions"]
        for x in f["ranges"]
    ]


async def test_coverage_start_snapshot_stop(odda_session) -> None:
    async with odda_session() as h, fixture_site(["coverage.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/coverage.html")
        await h.wait_for(bid, tid, "typeof window.__oddaCoverageFixture === 'function'")

        r = await h.call("coverage_start", {"browser_id": bid, "tab_id": tid})
        assert r["status"] == "recording"

        out = await h.eval(bid, tid, "String(window.__oddaCoverageFixture(true))")
        assert out == "taken-branch"

        r = await h.call("coverage_snapshot", {"browser_id": bid, "tab_id": tid})
        urls = [s["url"] for s in r["scripts"] if s.get("url")]
        counts = _fixture_counts(r)
        assert any("coverage.html" in u for u in urls)
        assert max(counts) > 0 and min(counts) == 0

        await h.eval(bid, tid, "String(window.__oddaCoverageFixture(true))")
        r = await h.call("coverage_stop", {"browser_id": bid, "tab_id": tid})
        counts = _fixture_counts(r)
        assert max(counts) >= 2 and min(counts) == 0

        err = await h.call_error("coverage_stop", {"browser_id": bid, "tab_id": tid})
        assert err == f"Tab {tid} is not recording coverage."

        err = await h.call_error("coverage_start", {"browser_id": bid, "tab_id": 9999})
        assert err == f"Tab 9999 not found in browser {bid}."
        err = await h.call_error(
            "coverage_start", {"browser_id": "zzzzz", "tab_id": tid}
        )
        assert err == "Browser zzzzz not found."


async def test_coverage_per_tab_and_survives_navigation(odda_session) -> None:
    async with odda_session() as h, fixture_site(["coverage.html"]) as fx:
        url = f"{fx.base}/coverage.html"
        bid, tid = await h.open_browser(url)
        await h.wait_for(bid, tid, "typeof window.__oddaCoverageFixture === 'function'")

        r = await h.call("tabs_open", {"browser_id": bid, "url": url})
        tid2 = r["tab_id"]
        await h.wait_for(
            bid, tid2, "typeof window.__oddaCoverageFixture === 'function'"
        )
        await h.call("coverage_start", {"browser_id": bid, "tab_id": tid2})
        await h.eval(bid, tid2, "String(window.__oddaCoverageFixture(true))")
        r = await h.call("coverage_stop", {"browser_id": bid, "tab_id": tid2})
        counts = _fixture_counts(r)
        assert max(counts) >= 1 and min(counts) == 0
        await h.call("tabs_close", {"browser_id": bid, "tab_id": tid2})

        # See ADR-0005.
        await h.call("coverage_start", {"browser_id": bid, "tab_id": tid})
        await h.navigate(bid, tid, url)
        await h.wait_for(bid, tid, "typeof window.__oddaCoverageFixture === 'function'")
        r = await h.call("coverage_snapshot", {"browser_id": bid, "tab_id": tid})
        nav_counts = _fixture_counts(r)
        nav_max = max(nav_counts)
        assert nav_max > 0

        await h.eval(bid, tid, "String(window.__oddaCoverageFixture(true))")
        r = await h.call("coverage_stop", {"browser_id": bid, "tab_id": tid})
        counts = _fixture_counts(r)
        assert max(counts) >= nav_max and min(counts) == 0
