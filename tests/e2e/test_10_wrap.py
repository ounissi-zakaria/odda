"""Wrap: install, list, remove, dump, clear (port of scrut 10-wrap.md).

A wrap is a named userscript that transparently records calls (or
property accesses) with receiver, args, ret, and stack. Functions in
args/ret serialize as opaque refs; large values truncate. Leaf-only
per ADR-0003. Records wipe on navigation; installations persist
(ADR-0004).
"""

from __future__ import annotations

import json

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
            {"browser_id": bid, "name": "jp", "expr": "JSON.parse"},
        )
        assert (r["name"], r["type"], r["expr"]) == ("jp", "call", "JSON.parse")

        r = await h.call_json("wrap_list", {"browser_id": bid})
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
            {"browser_id": bid, "name": "jp", "expr": "JSON.parse"},
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
    browser and non-existent-wrap errors are verbatim; wrap
    install/list/remove work with zero open tabs."""
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
                {"browser_id": bid, "name": name, "expr": expr},
            )
        await h.call(
            "wrap_access_add",
            {
                "browser_id": bid,
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
        r = await h.call("wrap_remove", {"browser_id": bid, "name": "jp"})
        assert (r["name"], r["removed"]) == ("jp", True)
        r = await h.call_json("wrap_list", {"browser_id": bid})
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

        # Errors: missing browser, non-existent wrap. (Install/remove
        # no longer validate a tab — they are browser-scope operations.)
        err = await h.call_error("wrap_list", {"browser_id": "zzzzz"})
        assert err == "Browser zzzzz not found."
        err = await h.call_error(
            "wrap_remove",
            {"browser_id": bid, "name": "no-such-wrap"},
        )
        assert err == "Userscript '__odda-wrap__no-such-wrap' not found"

        # Wrap install/list/remove are browser-scope operations
        # (ADR-0010): they must work with zero open tabs instead of
        # erroring on a missing tab.
        await h.call("tabs_close", {"browser_id": bid, "tab_id": tid})
        r = await h.call_json("wrap_list", {"browser_id": bid})
        assert sorted(w["name"] for w in r) == ["ael", "big", "ih"]
        await h.call(
            "wrap_calls_add",
            {"browser_id": bid, "name": "post", "expr": "Array.of"},
        )
        r = await h.call_json("wrap_list", {"browser_id": bid})
        assert sorted(w["name"] for w in r) == ["ael", "big", "ih", "post"]
        r = await h.call("wrap_remove", {"browser_id": bid, "name": "post"})
        assert (r["name"], r["removed"]) == ("post", True)
        r = await h.call_json("wrap_list", {"browser_id": bid})
        assert sorted(w["name"] for w in r) == ["ael", "big", "ih"]


async def test_wrap_access_data_slots_and_page_defined_props(odda_session) -> None:
    """Access wraps record plain data slots and properties the page
    defines later: an own data slot converts to a recording accessor
    (the value round-trips), an inherited data slot records on the
    owner without touching the prototype, and a not-yet-defined
    property is pre-armed so the page's load-time write records. A
    pre-armed property answers `in` but stays out of Object.keys
    until its first write flips it enumerable, like an assignment
    would."""
    body = (
        "<!doctype html><html><body><script>"
        "window.__oddaLib = 'page-set';"
        "</script></body></html>"
    )
    async with (
        odda_session() as h,
        fixture_site(index_body=body) as fx,
    ):
        url = f"{fx.base}/"
        bid, tid = await h.open_browser(url)

        # "000-*" sorts before "__odda-wrap__*", so these exist as plain
        # data slots at document_start, before the wrap userscripts run.
        await h.call(
            "userscript_install",
            {
                "browser_id": bid,
                "name": "000-setup",
                "source": (
                    "window.__oddaOwn = 42;"
                    "window.__oddaBase = {shared: 1};"
                    "window.__oddaKid = Object.create(window.__oddaBase);"
                ),
            },
        )
        for name, expr in (
            ("lib", "window.__oddaLib"),
            ("own", "window.__oddaOwn"),
            ("kid", "window.__oddaKid.shared"),
            ("never", "window.__oddaNever"),
        ):
            r = await h.call(
                "wrap_access_add", {"browser_id": bid, "name": name, "expr": expr}
            )
            assert (r["name"], r["type"]) == (name, "access")

        await h.navigate(bid, tid, url)
        await h.wait_for(bid, tid, "window.__oddaLib === 'page-set'")

        # Pre-armed: the page's own load-time write recorded as a set
        # with the written value in args.
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        lib = [x for x in r if x["wrap"] == "lib"]
        assert any(x["args"] == ["page-set"] and x["ret"] is None for x in lib)

        # Own data slot: the pre-existing value survives the conversion
        # and later reads/writes record (get: args [] + ret value).
        out = await h.eval(bid, tid, "String(window.__oddaOwn)")
        assert out == "42"
        await h.eval(bid, tid, "window.__oddaOwn = 43; String(window.__oddaOwn)")
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        own = [x for x in r if x["wrap"] == "own"]
        assert any(x["args"] == [] and x["ret"] == 42 for x in own)
        assert any(x["args"] == [43] and x["ret"] is None for x in own)
        assert any(x["args"] == [] and x["ret"] == 43 for x in own)

        # Inherited data slot: accesses record on the owner; the write
        # shadows (assignment semantics) and the prototype keeps its
        # value.
        await h.eval(
            bid, tid, "window.__oddaKid.shared = 2; String(window.__oddaKid.shared)"
        )
        out = await h.eval(bid, tid, "String(window.__oddaBase.shared)")
        assert out == "1"
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        kid = [x for x in r if x["wrap"] == "kid"]
        assert any(x["args"] == [2] and x["ret"] is None for x in kid)
        assert any(x["args"] == [] and x["ret"] == 2 for x in kid)

        # Pre-arm enumerability: never-written is reachable via `in`
        # but absent from Object.keys; the once-written property is
        # listed, as an assignment-created property would be.
        out = await h.eval(
            bid,
            tid,
            "JSON.stringify({"
            "neverIn: '__oddaNever' in window,"
            "neverListed: Object.keys(window).indexOf('__oddaNever') >= 0,"
            "libListed: Object.keys(window).indexOf('__oddaLib') >= 0})",
        )
        assert json.loads(out) == {
            "neverIn": True,
            "neverListed": False,
            "libListed": True,
        }


async def test_wrap_access_frozen_targets_stay_unwrapped(odda_session) -> None:
    """Frozen owners and pinned (non-writable, non-configurable) data
    slots offer no interception point: the wrap installs but records
    nothing, and the page keeps working with values intact."""
    body = "<!doctype html><html><body>ok</body></html>"
    async with (
        odda_session() as h,
        fixture_site(index_body=body) as fx,
    ):
        url = f"{fx.base}/"
        bid, tid = await h.open_browser(url)
        await h.call(
            "userscript_install",
            {
                "browser_id": bid,
                "name": "000-setup",
                "source": (
                    "window.__oddaFrozen = Object.freeze({pinned: 1});"
                    "Object.defineProperty(window, '__oddaPinned', "
                    "{value: 7, writable: false, configurable: false,"
                    " enumerable: true});"
                ),
            },
        )
        for name, expr in (
            ("fz", "window.__oddaFrozen.pinned"),
            ("pin", "window.__oddaPinned"),
        ):
            await h.call(
                "wrap_access_add", {"browser_id": bid, "name": name, "expr": expr}
            )
        await h.navigate(bid, tid, url)
        await h.wait_for(bid, tid, "window.__oddaFrozen && window.__oddaPinned === 7")

        # Sloppy-mode writes to the frozen/pinned slots are silent
        # no-ops; nothing records.
        out = await h.eval(
            bid,
            tid,
            "window.__oddaFrozen.pinned = 9; window.__oddaPinned = 9;"
            "JSON.stringify([window.__oddaFrozen.pinned, window.__oddaPinned])",
        )
        assert json.loads(out) == [1, 7]
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert not any(x["wrap"] in ("fz", "pin") for x in r)


async def test_wrap_access_proto_data_slot_is_per_receiver(odda_session) -> None:
    """A data-slot wrap on a prototype keeps per-receiver values: one
    instance's write must not leak into another instance or the
    prototype itself (an assignment would have created an own
    property on the writing instance), and unwritten receivers keep
    the slot's original value."""
    body = "<!doctype html><html><body>ok</body></html>"
    async with (
        odda_session() as h,
        fixture_site(index_body=body) as fx,
    ):
        url = f"{fx.base}/"
        bid, tid = await h.open_browser(url)
        await h.call(
            "userscript_install",
            {
                "browser_id": bid,
                "name": "000-setup",
                "source": (
                    "window.__oddaProto = {flag: 1};"
                    "window.__oddaA = Object.create(window.__oddaProto);"
                    "window.__oddaB = Object.create(window.__oddaProto);"
                ),
            },
        )
        await h.call(
            "wrap_access_add",
            {"browser_id": bid, "name": "flag", "expr": "window.__oddaProto.flag"},
        )
        await h.navigate(bid, tid, url)
        await h.wait_for(bid, tid, "window.__oddaProto && window.__oddaA")

        await h.eval(bid, tid, "window.__oddaA.flag = 2")
        out = await h.eval(
            bid,
            tid,
            "JSON.stringify([window.__oddaA.flag, window.__oddaB.flag,"
            " window.__oddaProto.flag])",
        )
        assert json.loads(out) == [2, 1, 1]
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        flag = [x for x in r if x["wrap"] == "flag"]
        assert any(x["args"] == [2] and x["ret"] is None for x in flag)
        assert any(x["args"] == [] and x["ret"] == 1 for x in flag)


async def test_wrap_access_owner_appearing_later(odda_session) -> None:
    """Wraps whose owner object does not exist at document_start arm
    the owner chain level by level (window.libX, then libX.modY) and
    start recording once the page creates the objects; a replaced
    owner is re-armed. Global lexical owners (let) never pass through
    window, so they stay unwatchable and silent."""
    body = (
        "<!doctype html><html><body><script>"
        "window.libX = {modY: {flag: 1}};"
        "let gobjL = {flag: 1}; gobjL.flag = 8;"
        "</script></body></html>"
    )
    async with (
        odda_session() as h,
        fixture_site(index_body=body) as fx,
    ):
        url = f"{fx.base}/"
        bid, tid = await h.open_browser(url)
        for name, expr in (
            ("late", "libX.modY.flag"),
            ("glet", "gobjL.flag"),
        ):
            await h.call(
                "wrap_access_add", {"browser_id": bid, "name": name, "expr": expr}
            )
        await h.navigate(bid, tid, url)
        await h.wait_for(bid, tid, "window.libX && window.libX.modY.flag === 1")

        await h.eval(
            bid, tid, "window.libX.modY.flag = 5; String(window.libX.modY.flag)"
        )
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        late = [x for x in r if x["wrap"] == "late"]
        assert any(x["args"] == [5] and x["ret"] is None for x in late)
        assert any(x["args"] == [] and x["ret"] == 5 for x in late)

        # Replacing the whole owner re-arms the wrap on the new object:
        # the literal's flag seeds the backing (read back as 9), and a
        # write on the new owner records.
        out = await h.eval(
            bid, tid, "window.libX = {modY: {flag: 9}}; String(window.libX.modY.flag)"
        )
        assert out == "9"
        await h.eval(bid, tid, "window.libX.modY.flag = 3")
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        late = [x for x in r if x["wrap"] == "late"]
        assert any(x["args"] == [] and x["ret"] == 9 for x in late)
        assert any(x["args"] == [3] and x["ret"] is None for x in late)

        # let-bound owners never surface on window: silent, no records.
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert not any(x["wrap"] == "glet" for x in r)


async def test_wrap_access_armed_chain_survives_churn(odda_session) -> None:
    """An armed chain that keeps failing to resolve (repeated
    non-object writes) must not multiply its watchers: after churn,
    the real owner still installs and records, and the page stays
    responsive."""
    body = (
        "<!doctype html><html><body><script>"
        "for (var i = 0; i < 30; i++) window.libX = null;"
        "window.libX = {modY: {flag: 2}};"
        "</script></body></html>"
    )
    async with (
        odda_session() as h,
        fixture_site(index_body=body) as fx,
    ):
        url = f"{fx.base}/"
        bid, tid = await h.open_browser(url)
        await h.call(
            "wrap_access_add",
            {"browser_id": bid, "name": "churn", "expr": "libX.modY.flag"},
        )
        await h.navigate(bid, tid, url)
        await h.wait_for(bid, tid, "window.libX && window.libX.modY.flag === 2")

        await h.eval(
            bid, tid, "window.libX.modY.flag = 6; String(window.libX.modY.flag)"
        )
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        churn = [x for x in r if x["wrap"] == "churn"]
        assert any(x["args"] == [6] and x["ret"] is None for x in churn)
        assert any(x["args"] == [] and x["ret"] == 6 for x in churn)


async def test_wrap_access_existing_prefix_replaced(odda_session) -> None:
    """An armed chain watches levels that already exist at
    document_start too: replacing an existing-but-incomplete owner
    (window.libX present, modY missing) re-arms on the new object."""
    async with (
        odda_session() as h,
        fixture_site(index_body="<!doctype html><html><body>ok</body></html>") as fx,
    ):
        url = f"{fx.base}/"
        bid, tid = await h.open_browser(url)
        await h.call(
            "userscript_install",
            {
                "browser_id": bid,
                "name": "000-setup",
                "source": "window.libX = {};",
            },
        )
        await h.call(
            "wrap_access_add",
            {"browser_id": bid, "name": "pfx", "expr": "libX.modY.flag"},
        )
        await h.navigate(bid, tid, url)
        await h.wait_for(bid, tid, "window.libX && typeof window.libX === 'object'")

        await h.eval(
            bid,
            tid,
            "window.libX = {modY: {flag: 1}}; window.libX.modY.flag = 4;"
            "String(window.libX.modY.flag)",
        )
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        pfx = [x for x in r if x["wrap"] == "pfx"]
        assert any(x["args"] == [4] and x["ret"] is None for x in pfx)
        assert any(x["args"] == [] and x["ret"] == 4 for x in pfx)


async def test_wrap_calls_owner_and_function_appearing_later(odda_session) -> None:
    """Call wraps on functions whose owner object or the function
    itself does not exist at document_start: both arm and record once
    the page defines them."""
    body = (
        "<!doctype html><html><body><script>"
        "window.libC = {modC: {calc: function (x) { return x * 2; }}};"
        "window.__laterFn = function (y) { return y + 1; };"
        "</script></body></html>"
    )
    async with (
        odda_session() as h,
        fixture_site(index_body=body) as fx,
    ):
        url = f"{fx.base}/"
        bid, tid = await h.open_browser(url)
        for name, expr in (
            ("calc", "libC.modC.calc"),
            ("lf", "__laterFn"),
        ):
            await h.call(
                "wrap_calls_add", {"browser_id": bid, "name": name, "expr": expr}
            )
        await h.navigate(bid, tid, url)
        await h.wait_for(
            bid,
            tid,
            "window.libC && typeof window.__laterFn === 'function'",
        )

        out = await h.eval(bid, tid, "String(window.libC.modC.calc(21))")
        assert out == "42"
        out = await h.eval(bid, tid, "String(window.__laterFn(1))")
        assert out == "2"
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        calc = [x for x in r if x["wrap"] == "calc"]
        assert any(x["args"] == [21] and x["ret"] == 42 for x in calc)
        lf = [x for x in r if x["wrap"] == "lf"]
        assert any(x["args"] == [1] and x["ret"] == 2 for x in lf)

        # A bare global written again with a new function keeps
        # recording (the converting pre-arm re-wraps on write).
        await h.eval(
            bid,
            tid,
            "window.__laterFn = function (z) { return z * 10; };"
            "String(window.__laterFn(4))",
        )
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        lf = [x for x in r if x["wrap"] == "lf"]
        assert any(x["args"] == [4] and x["ret"] == 40 for x in lf)

        # Self-assignment must not wrap the wrapper: exactly one
        # record per call afterwards.
        await h.eval(
            bid,
            tid,
            "window.__laterFn = window.__laterFn; String(window.__laterFn(7))",
        )
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        lf7 = [x for x in r if x["wrap"] == "lf" and x["args"] == [7]]
        assert len(lf7) == 1 and lf7[0]["ret"] == 70


async def test_wrap_records_page_declared_globals(odda_session) -> None:
    """A top-level function or var DECLARATION does not assign through
    window — the engine defines the property, clobbering any pre-armed
    pair. Both wrap kinds re-check after load, so page-declared globals
    record like assignment-defined ones."""
    body = (
        "<!doctype html><html><body><script>"
        "function jsAlert() { return 'alerted'; }"
        "var declVar = 5;"
        "</script></body></html>"
    )
    async with (
        odda_session() as h,
        fixture_site(index_body=body) as fx,
    ):
        url = f"{fx.base}/"
        bid, tid = await h.open_browser(url)
        await h.call(
            "wrap_calls_add",
            {"browser_id": bid, "name": "jsa", "expr": "jsAlert"},
        )
        await h.call(
            "wrap_access_add",
            {"browser_id": bid, "name": "dv", "expr": "window.declVar"},
        )
        await h.navigate(bid, tid, url)
        await h.wait_for(bid, tid, "typeof jsAlert === 'function' && declVar === 5")

        out = await h.eval(bid, tid, "String(jsAlert())")
        assert out == "alerted"
        await h.eval(bid, tid, "window.declVar = 6; String(window.declVar)")
        r = await h.call_json("wrap_dump", {"browser_id": bid, "tab_id": tid})
        jsa = [x for x in r if x["wrap"] == "jsa"]
        assert any(x["args"] == [] and x["ret"] == "alerted" for x in jsa)
        dv = [x for x in r if x["wrap"] == "dv"]
        assert any(x["args"] == [6] and x["ret"] is None for x in dv)
        assert any(x["args"] == [] and x["ret"] == 6 for x in dv)
