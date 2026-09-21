"""Port of scrut 04-tab-lifecycle.md: tab lifecycle, multi-browser, and ID
monotonicity.

Tab and browser IDs are integers, monotonic, and never reused. A closed
tab's id is retired forever. The browser stays alive with zero tabs
after its last tab is closed. Multiple browsers are isolated. Deviation
from the scrut doc: the multi-browser isolation check navigates browser
2 to the local fixture site (both browsers hit distinct pages there)
instead of https://example.org — the e2e run must not depend on real
internet access; isolation is proven by tab counts and independent
titles.
"""

from __future__ import annotations

import re

from tests.e2e.conftest import fixture_site


def _tab_total(rows: list[dict]) -> int:
    return sum(len(b["tabs"]) for b in rows)


async def test_tab_lifecycle_and_id_monotonicity(odda_session) -> None:
    """tabs_open returns the new tab id and status 'opened'; ids are
    strictly monotonic and never reused; closing all tabs leaves the
    browser alive with zero tabs, and a zero-tab browser can still open
    new tabs."""
    async with odda_session() as h, fixture_site(["index.html", "dialogs.html"]) as fx:
        bid, tid = await h.open_browser()
        assert tid == 1
        assert _tab_total(await h.call_json("tabs_list", {})) == 1

        # tabs_open --url → tab_id 2, status opened.
        r = await h.call("tabs_open", {"browser_id": bid, "url": f"{fx.base}/"})
        assert r == {"browser_id": bid, "tab_id": 2, "status": "opened"}
        assert _tab_total(await h.call_json("tabs_list", {})) == 2

        # tabs_open without url → blank tab, id 3.
        r = await h.call("tabs_open", {"browser_id": bid})
        assert r == {"browser_id": bid, "tab_id": 3, "status": "opened"}
        assert _tab_total(await h.call_json("tabs_list", {})) == 3

        # tabs_close → status closed, count drops.
        r = await h.call("tabs_close", {"browser_id": bid, "tab_id": 2})
        assert r == {"browser_id": bid, "tab_id": 2, "status": "closed"}
        assert _tab_total(await h.call_json("tabs_list", {})) == 2

        # The closed tab's id stays dead.
        err = await h.call_error("eval", {"browser_id": bid, "tab_id": 2, "js": "1"})
        assert err == f"Tab 2 not found in browser {bid}."

        # New tab ids are strictly higher than any previous id.
        r = await h.call("tabs_open", {"browser_id": bid, "url": f"{fx.base}/"})
        assert r["tab_id"] == 4
        assert r["status"] == "opened"
        err = await h.call_error("eval", {"browser_id": bid, "tab_id": 2, "js": "1"})
        assert err == f"Tab 2 not found in browser {bid}."

        # Closing all tabs leaves the browser alive with zero tabs.
        for t in (1, 3, 4):
            await h.call("tabs_close", {"browser_id": bid, "tab_id": t})
        r = await h.call_json("tabs_list", {"browser_id": bid})
        assert r == [{"browser_id": bid, "tabs": []}]

        # A browser with zero tabs can still open new tabs (id keeps
        # climbing past every retired id).
        r = await h.call("tabs_open", {"browser_id": bid, "url": f"{fx.base}/"})
        assert r == {"browser_id": bid, "tab_id": 5, "status": "opened"}
        r = await h.call_json("tabs_list", {"browser_id": bid})
        assert len(r[0]["tabs"]) == 1


async def test_multiple_browsers_isolated_and_ids_never_reused(
    odda_session,
) -> None:
    """Two browsers are isolated (independent tab counts and titles), and a
    closed browser_id is never reused: navigate on it errors."""
    async with odda_session() as h, fixture_site(["index.html", "dialogs.html"]) as fx:
        bid1, _tid1 = await h.open_browser()
        await h.call("browser_close", {"browser_id": bid1})

        # Open two more; each gets a fresh token and a fresh tab 1.
        bid2, tid2 = await h.open_browser()
        assert tid2 == 1
        assert bid2 != bid1

        # Navigate browser 2 to a local page (not example.org — no real
        # internet dependency in the e2e run).
        r = await h.navigate(bid2, tid2, f"{fx.base}/")
        assert r == {"status": f"Navigated to: {fx.base}/"}
        assert _tab_total(await h.call_json("tabs_list", {})) == 1

        bid3, tid3 = await h.open_browser()
        assert tid3 == 1
        assert bid3 not in (bid1, bid2)
        assert len(await h.call_json("tabs_list", {})) == 2

        # Operating on browser 2 doesn't affect browser 3: independent
        assert await h.eval(bid3, tid3, "document.title") == ""
        assert await h.eval(bid2, tid2, "document.title") == "Listener Test"
        rows = {
            b["browser_id"]: len(b["tabs"]) for b in await h.call_json("tabs_list", {})
        }
        assert rows == {bid2: 1, bid3: 1}

        await h.call("browser_close", {"browser_id": bid3})
        await h.call("browser_close", {"browser_id": bid2})

        # A closed browser_id is not reused.
        err = await h.call_error(
            "navigate", {"browser_id": bid2, "tab_id": 1, "url": "http://x"}
        )
        assert err == f"Browser {bid2} not found."
