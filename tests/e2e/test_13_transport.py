from __future__ import annotations

from tests.e2e.conftest import fixture_site


async def test_large_eval_response_succeeds(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        text = await h.eval(bid, tid, "'x'.repeat(70000)")
        assert len(text) >= 70000


async def test_large_wrap_dump_succeeds(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        await h.call(
            "wrap_calls_add",
            {"browser_id": bid, "name": "big", "expr": "JSON.parse"},
        )
        await h.navigate(bid, tid, f"{fx.base}/")

        inject = (
            "window.__oddaWrap = [];"
            "for (var i = 0; i < 300; i++) {"
            "  window.__oddaWrap.push({wrap: 'big', type: 'call',"
            "    this: null, args: ['{\"k\":' + i + '}'], ret: {v: i},"
            "    stack: [{fn: 'f', url: 'http://x/b.js', line: i, col: 1}]});"
            "}"
            "window.__oddaWrap.length;"
        )
        assert await h.eval(bid, tid, inject) == "300"

        dump = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert isinstance(dump, list)
        assert len(dump) == 300


async def test_wrap_dump_name_filters_server_side(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        await h.call(
            "wrap_calls_add",
            {"browser_id": bid, "name": "big", "expr": "JSON.parse"},
        )
        await h.navigate(bid, tid, f"{fx.base}/")

        inject = (
            "window.__oddaWrap = [];"
            "for (var i = 0; i < 300; i++) {"
            "  window.__oddaWrap.push({wrap: 'big', type: 'call',"
            "    this: null, args: ['{\"k\":' + i + '}'], ret: {v: i},"
            "    stack: [{fn: 'f', url: 'http://x/b.js', line: i, col: 1}]});"
            "}"
            "window.__oddaWrap.length;"
        )
        assert await h.eval(bid, tid, inject) == "300"

        inject2 = (
            "window.__oddaWrap.push({wrap: 'other', type: 'call',"
            "  this: null, args: [], ret: null, stack: []});"
            "window.__oddaWrap.length;"
        )
        assert await h.eval(bid, tid, inject2) == "301"

        big = await h.call(
            "wrap_dump", {"browser_id": bid, "tab_id": tid, "name": "big"}
        )
        assert len(big) == 300
        assert all(r["wrap"] == "big" for r in big)

        other = await h.call(
            "wrap_dump", {"browser_id": bid, "tab_id": tid, "name": "other"}
        )
        assert len(other) == 1
        assert all(r["wrap"] == "other" for r in other)

        assert (
            await h.call(
                "wrap_dump", {"browser_id": bid, "tab_id": tid, "name": "no-such-wrap"}
            )
            == []
        )

        err = await h.call_error(
            "wrap_dump", {"browser_id": bid, "tab_id": 9999, "name": "big"}
        )
        assert err == f"Tab 9999 not found in browser {bid}."


async def test_circular_eval_does_not_kill_server(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        r = await h.client.call_tool(
            "eval",
            {"browser_id": bid, "tab_id": tid, "js": "var a = {}; a.self = a; a"},
        )
        assert r.is_error

        assert await h.eval(bid, tid, "1 + 1") == "2"
