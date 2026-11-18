# See ADR-0003, ADR-0004.
from __future__ import annotations

import json

from tests.e2e.conftest import fixture_site


async def test_wrap_call_access_records_and_clears(odda_session) -> None:
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

        r = await h.call("wrap_list", {"browser_id": bid})
        assert [(w["name"], w["type"], w["expr"]) for w in r] == [
            ("jp", "call", "JSON.parse")
        ]

        await h.navigate(bid, tid, url)
        await h.wait_for(bid, tid, "typeof window.__oddaWrapFixture === 'function'")

        await h.eval(
            bid, tid, "String(window.__oddaWrapParseAndReturn('{\"a\": 1}').a)"
        )
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert len(r) >= 1
        rec = r[0]
        assert rec["wrap"] == "jp" and rec["type"] == "call"
        assert rec["args"] == ['{"a": 1}']
        assert rec["ret"] == {"a": 1}
        assert isinstance(rec["stack"], list) and len(rec["stack"]) >= 1
        assert all(set(f) == {"fn", "url", "line", "col"} for f in rec["stack"])

        # See ADR-0003.
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
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
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
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        ih = [x for x in r if x["wrap"] == "ih" and x["type"] == "access"]
        assert ih
        assert ih[0]["args"] == ["<b>hi</b>"]
        assert ih[0]["ret"] is None
        assert "this" in ih[0] and isinstance(ih[0]["stack"], list)

        r = await h.call("wrap_clear", {"browser_id": bid, "tab_id": tid})
        assert r["status"] == "cleared" and r["count"] >= 0
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert r == []


async def test_wrap_records_wipe_installation_persists_iframes(
    odda_session,
) -> None:
    # See ADR-0004.
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

        await h.eval(
            bid, tid, "String(window.__oddaWrapParseAndReturn('{\"x\": 1}').x)"
        )
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert any(x["wrap"] == "jp" for x in r)
        await h.navigate(bid, tid, url)
        await h.wait_for(bid, tid, "typeof window.__oddaWrapFixture === 'function'")
        await h.call("wrap_clear", {"browser_id": bid, "tab_id": tid})
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert r == []
        await h.eval(
            bid, tid, "String(window.__oddaWrapParseAndReturn('{\"y\": 2}').y)"
        )
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert any(x["wrap"] == "jp" for x in r)

        await h.call("wrap_clear", {"browser_id": bid, "tab_id": tid})
        await h.eval(
            bid,
            tid,
            "var f = document.getElementById('xframe'); "
            "var inner = f.contentWindow; "
            "inner.JSON.parse('{\"inf\": 1}'); 'ok'",
        )
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert any(x["wrap"] == "jp" for x in r)


async def test_wrap_truncation_remove_and_errors(odda_session) -> None:
    # See ADR-0040.
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

        await h.eval(
            bid,
            tid,
            "Array.of.apply(null, new Array(150).fill(0)"
            ".map(function(_, i) { return i; })); 'ok'",
        )
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        big = [x for x in r if x["wrap"] == "big"]
        assert big
        assert big[0]["ret"]["type"] == "array"
        assert big[0]["ret"]["truncated"] is True
        assert big[0]["ret"]["length"] == 150

        r = await h.call("wrap_remove", {"browser_id": bid, "name": "jp"})
        assert (r["name"], r["removed"]) == ("jp", True)
        r = await h.call("wrap_list", {"browser_id": bid})
        assert sorted(w["name"] for w in r) == ["ael", "big", "ih"]

        await h.navigate(bid, tid, url)
        await h.wait_for(bid, tid, "typeof window.__oddaWrapFixture === 'function'")
        await h.eval(
            bid, tid, "String(window.__oddaWrapParseAndReturn('{\"z\": 3}').z)"
        )
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert not any(x["wrap"] == "jp" for x in r)
        await h.eval(bid, tid, "String(window.__oddaWrapSetSink('<i>bye</i>'))")
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert any(x["wrap"] == "ih" for x in r)

        err = await h.call_error("wrap_list", {"browser_id": "zzzzz"})
        assert err == "Browser zzzzz not found."
        err = await h.call_error(
            "wrap_remove",
            {"browser_id": bid, "name": "no-such-wrap"},
        )
        assert err == "Userscript '__odda-wrap__no-such-wrap' not found"

        # See ADR-0040, ADR-0010.
        r = await h.call("tabs_close", {"browser_id": bid, "tab_id": tid})
        assert r["browser_closed"] is True
        rows = await h.call("browser_list", {})
        assert rows == [{"browser_id": bid, "state": "closed"}]


async def test_wrap_access_data_slots_and_page_defined_props(odda_session) -> None:
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

        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        lib = [x for x in r if x["wrap"] == "lib"]
        assert any(x["args"] == ["page-set"] and x["ret"] is None for x in lib)

        out = await h.eval(bid, tid, "String(window.__oddaOwn)")
        assert out == "42"
        await h.eval(bid, tid, "window.__oddaOwn = 43; String(window.__oddaOwn)")
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        own = [x for x in r if x["wrap"] == "own"]
        assert any(x["args"] == [] and x["ret"] == 42 for x in own)
        assert any(x["args"] == [43] and x["ret"] is None for x in own)
        assert any(x["args"] == [] and x["ret"] == 43 for x in own)

        await h.eval(
            bid, tid, "window.__oddaKid.shared = 2; String(window.__oddaKid.shared)"
        )
        out = await h.eval(bid, tid, "String(window.__oddaBase.shared)")
        assert out == "1"
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        kid = [x for x in r if x["wrap"] == "kid"]
        assert any(x["args"] == [2] and x["ret"] is None for x in kid)
        assert any(x["args"] == [] and x["ret"] == 2 for x in kid)

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

        out = await h.eval(
            bid,
            tid,
            "window.__oddaFrozen.pinned = 9; window.__oddaPinned = 9;"
            "JSON.stringify([window.__oddaFrozen.pinned, window.__oddaPinned])",
        )
        assert json.loads(out) == [1, 7]
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert not any(x["wrap"] in ("fz", "pin") for x in r)


async def test_wrap_access_proto_data_slot_is_per_receiver(odda_session) -> None:
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
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        flag = [x for x in r if x["wrap"] == "flag"]
        assert any(x["args"] == [2] and x["ret"] is None for x in flag)
        assert any(x["args"] == [] and x["ret"] == 1 for x in flag)


async def test_wrap_access_owner_appearing_later(odda_session) -> None:
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
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        late = [x for x in r if x["wrap"] == "late"]
        assert any(x["args"] == [5] and x["ret"] is None for x in late)
        assert any(x["args"] == [] and x["ret"] == 5 for x in late)

        out = await h.eval(
            bid, tid, "window.libX = {modY: {flag: 9}}; String(window.libX.modY.flag)"
        )
        assert out == "9"
        await h.eval(bid, tid, "window.libX.modY.flag = 3")
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        late = [x for x in r if x["wrap"] == "late"]
        assert any(x["args"] == [] and x["ret"] == 9 for x in late)
        assert any(x["args"] == [3] and x["ret"] is None for x in late)

        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        assert not any(x["wrap"] == "glet" for x in r)


async def test_wrap_access_armed_chain_survives_churn(odda_session) -> None:
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
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        churn = [x for x in r if x["wrap"] == "churn"]
        assert any(x["args"] == [6] and x["ret"] is None for x in churn)
        assert any(x["args"] == [] and x["ret"] == 6 for x in churn)


async def test_wrap_access_existing_prefix_replaced(odda_session) -> None:
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
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        pfx = [x for x in r if x["wrap"] == "pfx"]
        assert any(x["args"] == [4] and x["ret"] is None for x in pfx)
        assert any(x["args"] == [] and x["ret"] == 4 for x in pfx)


async def test_wrap_calls_owner_and_function_appearing_later(odda_session) -> None:
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
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        calc = [x for x in r if x["wrap"] == "calc"]
        assert any(x["args"] == [21] and x["ret"] == 42 for x in calc)
        lf = [x for x in r if x["wrap"] == "lf"]
        assert any(x["args"] == [1] and x["ret"] == 2 for x in lf)

        await h.eval(
            bid,
            tid,
            "window.__laterFn = function (z) { return z * 10; };"
            "String(window.__laterFn(4))",
        )
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        lf = [x for x in r if x["wrap"] == "lf"]
        assert any(x["args"] == [4] and x["ret"] == 40 for x in lf)

        await h.eval(
            bid,
            tid,
            "window.__laterFn = window.__laterFn; String(window.__laterFn(7))",
        )
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        lf7 = [x for x in r if x["wrap"] == "lf" and x["args"] == [7]]
        assert len(lf7) == 1 and lf7[0]["ret"] == 70


async def test_wrap_records_page_declared_globals(odda_session) -> None:
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
        r = await h.call("wrap_dump", {"browser_id": bid, "tab_id": tid})
        jsa = [x for x in r if x["wrap"] == "jsa"]
        assert any(x["args"] == [] and x["ret"] == "alerted" for x in jsa)
        dv = [x for x in r if x["wrap"] == "dv"]
        assert any(x["args"] == [6] and x["ret"] is None for x in dv)
        assert any(x["args"] == [] and x["ret"] == 6 for x in dv)
