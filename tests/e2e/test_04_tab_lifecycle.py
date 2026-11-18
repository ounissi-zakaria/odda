# See ADR-0040.
from __future__ import annotations

import re

from tests.e2e.conftest import fixture_site


def _tab_total(rows: list[dict]) -> int:
    return sum(len(b["tabs"]) for b in rows)


async def test_tab_lifecycle_and_id_monotonicity(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html", "dialogs.html"]) as fx:
        bid, tid = await h.open_browser()
        assert tid == 1
        assert _tab_total(await h.call("tabs_list", {})) == 1

        r = await h.call("tabs_open", {"browser_id": bid, "url": f"{fx.base}/"})
        assert r == {"browser_id": bid, "tab_id": 2, "status": "opened"}
        assert _tab_total(await h.call("tabs_list", {})) == 2

        r = await h.call("tabs_open", {"browser_id": bid})
        assert r == {"browser_id": bid, "tab_id": 3, "status": "opened"}
        assert _tab_total(await h.call("tabs_list", {})) == 3

        r = await h.call("tabs_close", {"browser_id": bid, "tab_id": 2})
        assert r == {"browser_id": bid, "tab_id": 2, "status": "closed"}
        assert _tab_total(await h.call("tabs_list", {})) == 2

        err = await h.call_error("eval", {"browser_id": bid, "tab_id": 2, "js": "1"})
        assert err == f"Tab 2 not found in browser {bid}."

        r = await h.call("tabs_open", {"browser_id": bid, "url": f"{fx.base}/"})
        assert r["tab_id"] == 4
        assert r["status"] == "opened"
        err = await h.call_error("eval", {"browser_id": bid, "tab_id": 2, "js": "1"})
        assert err == f"Tab 2 not found in browser {bid}."

        # See ADR-0040.
        for t in (1, 3):
            await h.call("tabs_close", {"browser_id": bid, "tab_id": t})
        r = await h.call("tabs_list", {})
        assert len(r[0]["tabs"]) == 1
        r = await h.call("tabs_close", {"browser_id": bid, "tab_id": 4})
        assert r["browser_closed"] is True
        err = await h.call_error("tabs_list", {"browser_id": bid})
        assert err == f"Browser {bid} is not open."


async def test_multiple_browsers_isolated_and_ids_never_reused(
    odda_session,
) -> None:
    async with odda_session() as h, fixture_site(["index.html", "dialogs.html"]) as fx:
        bid1, _tid1 = await h.open_browser()
        await h.call("browser_close", {"browser_id": bid1})

        bid2, tid2 = await h.open_browser()
        assert tid2 == 1
        assert bid2 != bid1

        r = await h.navigate(bid2, tid2, f"{fx.base}/")
        assert r == {"status": f"Navigated to: {fx.base}/"}
        assert _tab_total(await h.call("tabs_list", {})) == 1

        bid3, tid3 = await h.open_browser()
        assert tid3 == 1
        assert bid3 not in (bid1, bid2)
        assert len(await h.call("tabs_list", {})) == 2

        assert await h.eval(bid3, tid3, "document.title") == ""
        assert await h.eval(bid2, tid2, "document.title") == "Listener Test"
        rows = {b["browser_id"]: len(b["tabs"]) for b in await h.call("tabs_list", {})}
        assert rows == {bid2: 1, bid3: 1}

        await h.call("browser_close", {"browser_id": bid3})
        await h.call("browser_close", {"browser_id": bid2})

        err = await h.call_error(
            "navigate", {"browser_id": bid2, "tab_id": 1, "url": "http://x"}
        )
        assert err == f"Browser {bid2} is not open."
