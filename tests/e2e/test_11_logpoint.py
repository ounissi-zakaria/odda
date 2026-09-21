"""Logpoint: plant, list, dump, clear, remove (port of scrut 11-logpoint.md).

A logpoint is a non-pausing observation at a source location (url,
line, col) whose condition evaluates the agent's expr in the paused
frame's scope and records the result. Persists until removed; records
wipe on navigation; does not survive tab close (per-tab-session).
"""

from __future__ import annotations

from tests.e2e.conftest import fixture_site


async def test_logpoint_plant_record_list_and_remove(odda_session) -> None:
    """plant → record → list → error record → accumulate → remove."""
    async with (
        odda_session() as h,
        fixture_site(["logpoint.html", "logpoint.js"]) as fx,
    ):
        js_url = f"{fx.base}/logpoint.js"
        url = f"{fx.base}/logpoint.html"
        bid, tid = await h.open_browser(url)
        await h.wait_for(bid, tid, "typeof window.__oddaLogpointFixture === 'function'")

        # Line 2 col 0 is the `return greeting;` statement; greeting was
        # assigned on line 1, so the logpoint records its value.
        r = await h.call(
            "logpoint_add",
            {
                "browser_id": bid,
                "tab_id": tid,
                "url": js_url,
                "line": 2,
                "col": 0,
                "expr": "greeting",
            },
        )
        assert r["status"] == "planted" and "id" in r and "warning" not in r

        out = await h.eval(bid, tid, "String(window.__oddaLogpointFixture('world'))")
        assert out == "hello world"

        r = await h.call_json("logpoint_dump", {"browser_id": bid, "tab_id": tid})
        assert len(r) >= 1
        rec = r[0]
        assert all(k in rec for k in ("logpoint", "url", "line", "col"))
        assert "/logpoint.js" in rec["url"]
        assert (rec["line"], rec["col"]) == (2, 0)
        assert rec["value"] == "hello world" and rec["error"] is None

        r = await h.call_json("logpoint_list", {"browser_id": bid, "tab_id": tid})
        assert len(r) == 1
        assert "/logpoint.js" in r[0]["url"]
        assert (r[0]["line"], r[0]["col"], r[0]["expr"]) == (2, 0, "greeting")

        # A wrong local name produces an error record, not silence.
        r = await h.call(
            "logpoint_add",
            {
                "browser_id": bid,
                "tab_id": tid,
                "url": js_url,
                "line": 1,
                "col": 0,
                "expr": "noSuchLocal",
            },
        )
        assert r["status"] == "planted"
        r = await h.call("logpoint_clear", {"browser_id": bid, "tab_id": tid})
        assert r["status"] == "cleared" and r["count"] >= 0
        await h.eval(bid, tid, "String(window.__oddaLogpointFixture('world'))")
        r = await h.call_json("logpoint_dump", {"browser_id": bid, "tab_id": tid})
        errs = [x for x in r if x.get("error")]
        assert errs and errs[0]["value"] is None
        assert "ReferenceError" in errs[0]["error"]

        # Not fire-once: records accumulate across hits on one page load.
        await h.call("logpoint_clear", {"browser_id": bid, "tab_id": tid})
        await h.eval(bid, tid, "String(window.__oddaLogpointFixture('first'))")
        await h.eval(bid, tid, "String(window.__oddaLogpointFixture('second'))")
        r = await h.call_json("logpoint_dump", {"browser_id": bid, "tab_id": tid})
        vals = [x for x in r if x.get("value") in ("hello first", "hello second")]
        assert len(vals) == 2

        # Records wipe on navigation; the CDP breakpoint re-binds and the
        # registry persists (ADR-0004).
        await h.navigate(bid, tid, url)
        await h.wait_for(bid, tid, "typeof window.__oddaLogpointFixture === 'function'")
        r = await h.call_json("logpoint_dump", {"browser_id": bid, "tab_id": tid})
        assert r == []
        await h.eval(bid, tid, "String(window.__oddaLogpointFixture('after-nav'))")
        r = await h.call_json("logpoint_dump", {"browser_id": bid, "tab_id": tid})
        assert any(x.get("value") == "hello after-nav" for x in r)
        r = await h.call_json("logpoint_list", {"browser_id": bid, "tab_id": tid})
        assert sorted(lp["expr"] for lp in r) == ["greeting", "noSuchLocal"]

        # Remove noSuchLocal by id; only greeting remains and records.
        r = await h.call_json("logpoint_list", {"browser_id": bid, "tab_id": tid})
        lpid = next(lp["id"] for lp in r if lp["expr"] == "noSuchLocal")
        r = await h.call(
            "logpoint_remove",
            {"browser_id": bid, "tab_id": tid, "lp_id": lpid},
        )
        assert (r["status"], r["id"]) == ("removed", lpid)
        r = await h.call_json("logpoint_list", {"browser_id": bid, "tab_id": tid})
        assert [lp["expr"] for lp in r] == ["greeting"]

        await h.call("logpoint_clear", {"browser_id": bid, "tab_id": tid})
        await h.navigate(bid, tid, url)
        await h.wait_for(bid, tid, "typeof window.__oddaLogpointFixture === 'function'")
        await h.eval(bid, tid, "String(window.__oddaLogpointFixture('after-remove'))")
        r = await h.call_json("logpoint_dump", {"browser_id": bid, "tab_id": tid})
        assert any(x.get("value") == "hello after-remove" for x in r)
        assert not any(x.get("error") for x in r)


async def test_logpoint_warnings_errors_and_tab_session(odda_session) -> None:
    """Stale-URL warning, duplicate-location / unknown-id / missing
    tab-browser errors, and the per-tab-session lifecycle."""
    async with (
        odda_session() as h,
        fixture_site(["logpoint.html", "logpoint.js"]) as fx,
    ):
        js_url = f"{fx.base}/logpoint.js"
        url = f"{fx.base}/logpoint.html"
        bid, tid = await h.open_browser(url)
        await h.wait_for(bid, tid, "typeof window.__oddaLogpointFixture === 'function'")
        await h.call(
            "logpoint_add",
            {
                "browser_id": bid,
                "tab_id": tid,
                "url": js_url,
                "line": 2,
                "col": 0,
                "expr": "greeting",
            },
        )

        # Stale URL: planted, but with a warning string.
        r = await h.call(
            "logpoint_add",
            {
                "browser_id": bid,
                "tab_id": tid,
                "url": f"{fx.base}/does-not-exist.js",
                "line": 0,
                "col": 0,
                "expr": "x",
            },
        )
        assert r["status"] == "planted" and isinstance(r.get("warning"), str)

        # Duplicate location errors; unknown id errors.
        err = await h.call_error(
            "logpoint_add",
            {
                "browser_id": bid,
                "tab_id": tid,
                "url": js_url,
                "line": 2,
                "col": 0,
                "expr": "greeting",
            },
        )
        assert "A logpoint already exists at" in err
        err = await h.call_error(
            "logpoint_remove",
            {"browser_id": bid, "tab_id": tid, "lp_id": "lp-nope"},
        )
        assert f"Logpoint lp-nope not found in tab {tid}." in err

        # Missing tab / browser.
        err = await h.call_error(
            "logpoint_add",
            {
                "browser_id": bid,
                "tab_id": 9999,
                "url": "http://x",
                "line": 0,
                "col": 0,
                "expr": "x",
            },
        )
        assert err == f"Tab 9999 not found in browser {bid}."
        err = await h.call_error(
            "logpoint_list", {"browser_id": "zzzzz", "tab_id": tid}
        )
        assert err == "Browser zzzzz not found."

        # Per-tab-session (ADR-0004): closing the tab drops the
        # installations; a fresh tab starts with an empty registry.
        await h.call("tabs_close", {"browser_id": bid, "tab_id": tid})
        r = await h.call("tabs_open", {"browser_id": bid, "url": url})
        tid = r["tab_id"]
        await h.wait_for(bid, tid, "typeof window.__oddaLogpointFixture === 'function'")
        r = await h.call_json("logpoint_list", {"browser_id": bid, "tab_id": tid})
        assert r == []
