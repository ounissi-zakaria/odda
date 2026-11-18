# See ADR-0010.
from __future__ import annotations

from tests.e2e.conftest import fixture_site

WRAP_PREFIX = "__odda-wrap__"


async def test_wrap_scope_is_per_browser(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid1, tid1 = await h.open_browser(f"{fx.base}/")
        bid2, tid2 = await h.open_browser(f"{fx.base}/")
        assert bid2 != bid1

        await h.call(
            "wrap_calls_add",
            {
                "browser_id": bid1,
                "name": "leaktest",
                "expr": "JSON.parse",
            },
        )

        r = await h.call("wrap_list", {"browser_id": bid2})
        assert r == []

        r = await h.call("wrap_list", {"browser_id": bid1})
        assert any(w["name"] == "leaktest" for w in r)

        await h.call(
            "wrap_calls_add",
            {
                "browser_id": bid2,
                "name": "b2wrap",
                "expr": "JSON.parse",
            },
        )
        r = await h.call("wrap_list", {"browser_id": bid1})
        assert not any(w["name"] == "b2wrap" for w in r)
        r = await h.call("wrap_list", {"browser_id": bid2})
        assert any(w["name"] == "b2wrap" for w in r)

        r = await h.call("userscript_list", {"browser_id": bid2})
        assert not any(s["name"] == WRAP_PREFIX + "leaktest" for s in r)


async def test_userscript_scope_is_per_browser(odda_session, tmp_path) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid1, tid1 = await h.open_browser(f"{fx.base}/")
        bid2, tid2 = await h.open_browser(f"{fx.base}/")

        us_file = tmp_path / "us1.js"
        us_file.write_text('window.__perBrowserUs__ = "browser-1";')
        await h.call(
            "userscript_install",
            {"browser_id": bid1, "name": "us1", "file": str(us_file)},
        )

        r = await h.call("userscript_list", {"browser_id": bid2})
        assert not any(s["name"] == "us1" for s in r)

        r = await h.call("userscript_list", {"browser_id": bid1})
        assert {"us1"} <= {s["name"] for s in r}

        await h.navigate(bid2, tid2, f"{fx.base}/")
        v = await h.wait_for(bid2, tid2, "typeof window.__perBrowserUs__", timeout=3)
        assert v == "undefined"

        await h.navigate(bid1, tid1, f"{fx.base}/")
        v = await h.wait_for(bid1, tid1, "window.__perBrowserUs__", timeout=3)
        assert v == "browser-1"


async def test_default_userscripts_ship_in_every_browser(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid1, tid1 = await h.open_browser(f"{fx.base}/")
        bid2, tid2 = await h.open_browser(f"{fx.base}/")

        await h.navigate(bid1, tid1, f"{fx.base}/")
        v = await h.wait_for(
            bid1, tid1, "window.__oddaLogpoint !== undefined", timeout=3
        )
        assert v == "true"

        await h.navigate(bid2, tid2, f"{fx.base}/")
        v = await h.wait_for(
            bid2, tid2, "window.__oddaLogpoint !== undefined", timeout=3
        )
        assert v == "true"
