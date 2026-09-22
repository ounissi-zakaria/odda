"""Per-browser scope: wraps and userscripts do not leak across browsers (port
of scrut 14-per-browser-scope.md).

Wraps and userscripts are stored per-browser (ADR-0010): a wrap or
userscript installed on browser 1 does not appear in browser 2's
wrap_list/userscript_list and does not run in browser 2's tabs. The
default userscripts (logpoint-helpers.js) ship in every browser's scope.
"""

from __future__ import annotations

from tests.e2e.conftest import fixture_site

WRAP_PREFIX = "__odda-wrap__"


async def test_wrap_scope_is_per_browser(odda_session) -> None:
    """A wrap on browser 1 is invisible in browser 2's wrap_list (and vice
    versa); browser 2's list starts empty of browser 1's wraps."""
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

        # Fresh browser 2: no wraps from browser 1's scope.
        r = await h.call_json("wrap_list", {"browser_id": bid2})
        assert r == []

        # Browser 1 still has its own wrap.
        r = await h.call_json("wrap_list", {"browser_id": bid1})
        assert any(w["name"] == "leaktest" for w in r)

        # A wrap installed on browser 2 does not appear on browser 1.
        await h.call(
            "wrap_calls_add",
            {
                "browser_id": bid2,
                "name": "b2wrap",
                "expr": "JSON.parse",
            },
        )
        r = await h.call_json("wrap_list", {"browser_id": bid1})
        assert not any(w["name"] == "b2wrap" for w in r)
        r = await h.call_json("wrap_list", {"browser_id": bid2})
        assert any(w["name"] == "b2wrap" for w in r)

        # With wraps installed on browser 1, browser 2's userscript_list must
        # not carry the wrap's userscript either (containment absence, not an
        # exact list — only default userscripts like logpoint-helpers.js
        # may appear).
        r = await h.call_json("userscript_list", {"browser_id": bid2})
        assert not any(s["name"] == WRAP_PREFIX + "leaktest" for s in r)


async def test_userscript_scope_is_per_browser(odda_session, tmp_path) -> None:
    """us1 installed on browser 1: absent from browser 2's userscript_list,
    present on browser 1's, and it only sets its marker in browser 1's tabs."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid1, tid1 = await h.open_browser(f"{fx.base}/")
        bid2, tid2 = await h.open_browser(f"{fx.base}/")

        us_file = tmp_path / "us1.js"
        us_file.write_text('window.__perBrowserUs__ = "browser-1";')
        await h.call(
            "userscript_install",
            {"browser_id": bid1, "name": "us1", "file": str(us_file)},
        )

        r = await h.call_json("userscript_list", {"browser_id": bid2})
        assert not any(s["name"] == "us1" for s in r)

        r = await h.call_json("userscript_list", {"browser_id": bid1})
        assert {"us1"} <= {s["name"] for s in r}

        # Behavioral: the userscript must not run in browser 2's tab.
        await h.navigate(bid2, tid2, f"{fx.base}/")
        v = await h.wait_for(bid2, tid2, "typeof window.__perBrowserUs__", timeout=3)
        assert v == "undefined"

        # It does run in browser 1's tab.
        await h.navigate(bid1, tid1, f"{fx.base}/")
        v = await h.wait_for(bid1, tid1, "window.__perBrowserUs__", timeout=3)
        assert v == "browser-1"


async def test_default_userscripts_ship_in_every_browser(odda_session) -> None:
    """logpoint-helpers.js is a default userscript: it runs in both browser
    1 and browser 2 without ever being installed on either."""
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
