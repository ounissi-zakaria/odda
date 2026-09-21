"""Wrap: install, list, remove, dump, clear (port of scrut 10-wrap.md).

A wrap is a named userscript that transparently records calls (or
property accesses) with receiver, args, ret, and stack. Functions in
args/ret serialize as opaque refs; large values truncate. Leaf-only
per ADR-0003. Records wipe on navigation; installations persist
(ADR-0004).
"""

from __future__ import annotations

from tests.e2e.conftest import fixture_site


async def test_wrap_call_access_records_and_clears(odda_session) -> None:
    """Call wrap records args/ret/stack; access wrap records the written
    value; function args become opaque refs; clear zeros the records."""
    async with (
        odda_session() as h,
        fixture_site(["wrap.html", "iframe-inner.html"]) as fx,
    ):
        url = f"{fx.base}/wrap.html"
        bid, tid = await h.open_browser(url)

        r = await h.call(
            "wrap_calls_add",
            {"browser_id": bid, "tab_id": tid, "name": "jp", "expr": "JSON.parse"},
        )
        assert (r["name"], r["type"], r["expr"]) == ("jp", "call", "JSON.parse")

        r = await h.call_json("wrap_list", {"browser_id": bid, "tab_id": tid})
        assert [(w["name"], w["type"], w["expr"]) for w in r] == [
            ("jp", "call", "JSON.parse")
        ]

        # The wrap userscript runs at document_start, so re-navigate.
        await h.navigate(bid, tid, url)
        await h.wait_for(bid, tid, "typeof window.__oddaWrapFixture === 'function'")

        await h.eval(
            bid, tid, "String(window.__oddaWrapParseAndReturn('{\"a\": 1}').a)"
        )
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert len(r) >= 1
        rec = r[0]
        assert rec["wrap"] == "jp" and rec["type"] == "call"
        assert rec["args"] == ['{"a": 1}']
        assert rec["ret"] == {"a": 1}
        assert isinstance(rec["stack"], list) and len(rec["stack"]) >= 1
        assert all(set(f) == {"fn", "url", "line", "col"} for f in rec["stack"])

        # Opaque function refs (ADR-0003 leaf-only): the callback is
        # recorded as {type, name, source}, never invoked/wrapped itself.
        await h.call(
            "wrap_calls_add",
            {
                "browser_id": bid,
                "tab_id": tid,
                "name": "ael",
                "expr": "EventTarget.prototype.addEventListener",
            },
        )
        await h.navigate(bid, tid, url)
        await h.wait_for(bid, tid, "typeof window.__oddaWrapFixture === 'function'")
        await h.eval(
            bid,
            tid,
            "String(window.__oddaWrapFixture("
            "document.body, 'click', function myHandler() {}))",
        )
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        ael = [
            x
            for x in r
            if x["wrap"] == "ael"
            and x["args"]
            and x["args"][0] == "click"
            and isinstance(x["args"][1], dict)
            and x["args"][1].get("name") == "myHandler"
        ]
        assert ael
        fn = ael[0]["args"][1]
        assert fn["type"] == "function"
        assert isinstance(fn.get("source"), str) and "myHandler" in fn["source"]
        assert ael[0]["ret"] is None

        # Access wrap: a set records args[0] as the value written, ret null.
        r = await h.call(
            "wrap_access_add",
            {
                "browser_id": bid,
                "tab_id": tid,
                "name": "ih",
                "expr": "HTMLElement.prototype.innerHTML",
            },
        )
        assert (r["name"], r["type"], r["expr"]) == (
            "ih",
            "access",
            "HTMLElement.prototype.innerHTML",
        )
        await h.navigate(bid, tid, url)
        await h.wait_for(bid, tid, "typeof window.__oddaWrapFixture === 'function'")
        await h.eval(bid, tid, "String(window.__oddaWrapSetSink('<b>hi</b>'))")
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        ih = [x for x in r if x["wrap"] == "ih" and x["type"] == "access"]
        assert ih
        assert ih[0]["args"] == ["<b>hi</b>"]
        assert ih[0]["ret"] is None
        assert "this" in ih[0] and isinstance(ih[0]["stack"], list)

        # Clear zeros the records without navigating.
        r = await h.call("wrap_clear", {"browser_id": bid, "tab_id": tid})
        assert r["status"] == "cleared" and r["count"] >= 0
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert r == []


async def test_wrap_records_wipe_installation_persists_iframes(
    odda_session,
) -> None:
    """Records wipe on navigation but the wrap re-installs (ADR-0004);
    wraps reach iframes and aggregate to the top frame."""
    async with (
        odda_session() as h,
        fixture_site(["wrap.html", "iframe-inner.html"]) as fx,
    ):
        url = f"{fx.base}/wrap.html"
        bid, tid = await h.open_browser(url)
        await h.call(
            "wrap_calls_add",
            {"browser_id": bid, "tab_id": tid, "name": "jp", "expr": "JSON.parse"},
        )
        await h.navigate(bid, tid, url)
        await h.wait_for(bid, tid, "typeof window.__oddaWrapFixture === 'function'")

        # Seed a record, then navigate: records wipe, installation persists.
        await h.eval(
            bid, tid, "String(window.__oddaWrapParseAndReturn('{\"x\": 1}').x)"
        )
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert any(x["wrap"] == "jp" for x in r)
        await h.navigate(bid, tid, url)
        await h.wait_for(bid, tid, "typeof window.__oddaWrapFixture === 'function'")
        # Playwright's own setup may record; clear any stragglers first.
        await h.call("wrap_clear", {"browser_id": bid, "tab_id": tid})
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert r == []
        await h.eval(
            bid, tid, "String(window.__oddaWrapParseAndReturn('{\"y\": 2}').y)"
        )
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert any(x["wrap"] == "jp" for x in r)

        # all_frames: true — the wrap runs in the iframe too, and
        # same-origin iframes aggregate records to the top frame.
        await h.call("wrap_clear", {"browser_id": bid, "tab_id": tid})
        await h.eval(
            bid,
            tid,
            "var f = document.getElementById('xframe'); "
            "var inner = f.contentWindow; "
            "inner.JSON.parse('{\"inf\": 1}'); 'ok'",
        )
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert any(x["wrap"] == "jp" for x in r)


async def test_wrap_truncation_remove_and_errors(odda_session) -> None:
    """Large arrays truncate; remove stops future recording; missing
    tab/browser and non-existent-wrap errors are verbatim."""
    async with (
        odda_session() as h,
        fixture_site(["wrap.html", "iframe-inner.html"]) as fx,
    ):
        url = f"{fx.base}/wrap.html"
        bid, tid = await h.open_browser(url)
        for name, expr in (
            ("jp", "JSON.parse"),
            ("big", "Array.of"),
            ("ael", "EventTarget.prototype.addEventListener"),
        ):
            await h.call(
                "wrap_calls_add",
                {"browser_id": bid, "tab_id": tid, "name": name, "expr": expr},
            )
        await h.call(
            "wrap_access_add",
            {
                "browser_id": bid,
                "tab_id": tid,
                "name": "ih",
                "expr": "HTMLElement.prototype.innerHTML",
            },
        )
        await h.navigate(bid, tid, url)
        await h.wait_for(bid, tid, "typeof window.__oddaWrapFixture === 'function'")

        # Arrays longer than 100 elements truncate to {type, truncated, length}.
        await h.eval(
            bid,
            tid,
            "Array.of.apply(null, new Array(150).fill(0)"
            ".map(function(_, i) { return i; })); 'ok'",
        )
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        big = [x for x in r if x["wrap"] == "big"]
        assert big
        assert big[0]["ret"]["type"] == "array"
        assert big[0]["ret"]["truncated"] is True
        assert big[0]["ret"]["length"] == 150

        # Remove jp; the others stay installed.
        r = await h.call(
            "wrap_remove", {"browser_id": bid, "tab_id": tid, "name": "jp"}
        )
        assert (r["name"], r["removed"]) == ("jp", True)
        r = await h.call_json("wrap_list", {"browser_id": bid, "tab_id": tid})
        assert sorted(w["name"] for w in r) == ["ael", "big", "ih"]

        # After a re-navigate the removed wrap no longer records; the
        # others still do.
        await h.navigate(bid, tid, url)
        await h.wait_for(bid, tid, "typeof window.__oddaWrapFixture === 'function'")
        await h.eval(
            bid, tid, "String(window.__oddaWrapParseAndReturn('{\"z\": 3}').z)"
        )
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert not any(x["wrap"] == "jp" for x in r)
        await h.eval(bid, tid, "String(window.__oddaWrapSetSink('<i>bye</i>'))")
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert any(x["wrap"] == "ih" for x in r)

        # Errors: missing tab, missing browser, non-existent wrap.
        err = await h.call_error(
            "wrap_calls_add",
            {"browser_id": bid, "tab_id": 9999, "name": "x", "expr": "JSON.parse"},
        )
        assert err == f"Tab 9999 not found in browser {bid}."
        err = await h.call_error("wrap_list", {"browser_id": "zzzzz", "tab_id": tid})
        assert err == "Browser zzzzz not found."
        err = await h.call_error(
            "wrap_remove",
            {"browser_id": bid, "tab_id": tid, "name": "no-such-wrap"},
        )
        assert err == "Userscript '__odda-wrap__no-such-wrap' not found"
