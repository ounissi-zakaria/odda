"""Transport: large responses and non-serializable eval returns (scrut 13).

Two JSON-RPC transport guarantees: arbitrarily large tool results (a
70KB eval, a 300-record wrap dump) come back intact — the transport's
readline limit is raised — and an eval returning a circular reference
errors on that call without killing the server (a follow-up eval still
succeeds).
"""

from __future__ import annotations

from tests.e2e.conftest import fixture_site


async def test_large_eval_response_succeeds(odda_session) -> None:
    """A single eval returning a >64KB string succeeds (readline limit raised)."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        text = await h.eval(bid, tid, "'x'.repeat(70000)")
        assert len(text) >= 70000


async def test_large_wrap_dump_succeeds(odda_session) -> None:
    """A 300-record wrap dump returns the full list in one call."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        await h.call(
            "wrap_calls_add",
            {"browser_id": bid, "name": "big", "expr": "JSON.parse"},
        )
        await h.navigate(bid, tid, f"{fx.base}/")

        # Inject 300 synthetic records directly into window.__oddaWrap
        # (single js string; the eval's value is the new record count).
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

        dump = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert isinstance(dump, list)
        assert len(dump) == 300


async def test_wrap_dump_name_filters_server_side(odda_session) -> None:
    """wrap_dump name= filters to one wrap's records; no match → []; missing tab errors."""
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

        # A second wrap's record sits alongside the first: 301 total.
        inject2 = (
            "window.__oddaWrap.push({wrap: 'other', type: 'call',"
            "  this: null, args: [], ret: null, stack: []});"
            "window.__oddaWrap.length;"
        )
        assert await h.eval(bid, tid, inject2) == "301"

        big = await h.call_json(
            "wrap_dump", {"browser_id": bid, "tab_id": tid, "name": "big"}
        )
        assert len(big) == 300
        assert all(r["wrap"] == "big" for r in big)

        other = await h.call_json(
            "wrap_dump", {"browser_id": bid, "tab_id": tid, "name": "other"}
        )
        assert len(other) == 1
        assert all(r["wrap"] == "other" for r in other)

        # No matching records → empty list.
        assert (
            await h.call_json(
                "wrap_dump", {"browser_id": bid, "tab_id": tid, "name": "no-such-wrap"}
            )
            == []
        )

        # Missing tab errors cleanly (message verbatim).
        err = await h.call_error(
            "wrap_dump", {"browser_id": bid, "tab_id": 9999, "name": "big"}
        )
        assert err == f"Tab 9999 not found in browser {bid}."


async def test_circular_eval_does_not_kill_server(odda_session) -> None:
    """A circular eval return errors on that call; the session stays alive."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        # The circular object cannot be JSON-serialized — the SDK's
        # crash path answers is_error. Use the client directly: this is
        # a crash-path result, not a ToolError with a message.
        r = await h.client.call_tool(
            "eval",
            {"browser_id": bid, "tab_id": tid, "js": "var a = {}; a.self = a; a"},
        )
        assert r.is_error

        # The server survived — a follow-up eval succeeds.
        assert await h.eval(bid, tid, "1 + 1") == "2"
