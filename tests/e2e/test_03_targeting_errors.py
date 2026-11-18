from __future__ import annotations

import re

from tests.e2e.conftest import fixture_site


async def test_navigate_errors_on_unknown_browser_id(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, _tid = await h.open_browser(f"{fx.base}/")
        err = await h.call_error(
            "navigate",
            {"browser_id": "zzzzz", "tab_id": 1, "url": "http://x"},
        )
        assert err == "Browser zzzzz not found."
        assert re.fullmatch(r"[a-z]{5}", bid)


async def test_navigate_errors_on_unknown_tab_id(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, _tid = await h.open_browser(f"{fx.base}/")
        err = await h.call_error(
            "navigate",
            {"browser_id": bid, "tab_id": 9999, "url": "http://x"},
        )
        assert err == f"Tab 9999 not found in browser {bid}."


async def test_eval_errors_on_unknown_tab_id(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, _tid = await h.open_browser(f"{fx.base}/")
        err = await h.call_error("eval", {"browser_id": bid, "tab_id": 9999, "js": "1"})
        assert err == f"Tab 9999 not found in browser {bid}."


async def test_screenshot_errors_on_unknown_browser_id(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        await h.open_browser(f"{fx.base}/")
        err = await h.call_error("screenshot", {"browser_id": "zzzzz", "tab_id": 1})
        assert err == "Browser zzzzz not found."


async def test_tabs_open_errors_on_unknown_browser_id(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        await h.open_browser(f"{fx.base}/")
        err = await h.call_error("tabs_open", {"browser_id": "zzzzz"})
        assert err == "Browser zzzzz not found."


async def test_tabs_close_errors_on_unknown_tab_id(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, _tid = await h.open_browser(f"{fx.base}/")
        err = await h.call_error("tabs_close", {"browser_id": bid, "tab_id": 9999})
        assert err == f"Tab 9999 not found in browser {bid}."


async def test_event_listeners_errors_on_unknown_browser_id(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        await h.open_browser(f"{fx.base}/")
        err = await h.call_error(
            "event_listeners", {"browser_id": "zzzzz", "tab_id": 1}
        )
        assert err == "Browser zzzzz not found."
