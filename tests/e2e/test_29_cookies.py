from __future__ import annotations

from urllib.parse import quote

from tests.e2e.conftest import dyn_server, odda_session


def _set_cookie_url(site: str, marker: str, *cookies: str) -> str:
    encoded = "&".join(f"header=set-cookie:{quote(c, safe='')}" for c in cookies)
    return f"{site}/?marker={marker}&{encoded}"


async def test_cookies_list_sees_httponly_that_js_cannot(odda_session) -> None:
    async with odda_session() as h, dyn_server() as dyn:
        bid, tid = await h.open_browser()
        url = _set_cookie_url(
            dyn.tls_base,
            "cookie-httponly",
            "odda_ht=1; HttpOnly; Path=/",
            "odda_plain=2; Path=/",
        )
        await h.navigate(bid, tid, url)

        jar = {c["name"]: c for c in await h.call("cookies_list", {"browser_id": bid})}
        assert jar["odda_ht"]["value"] == "1"
        assert jar["odda_ht"]["httpOnly"] is True
        assert jar["odda_ht"]["domain"] == "127.0.0.1"
        assert jar["odda_plain"]["value"] == "2"
        assert jar["odda_plain"]["httpOnly"] is False

        visible = await h.eval(bid, tid, "document.cookie")
        assert visible == "odda_plain=2"


async def test_cookies_clear_name_filter_spares_siblings(odda_session) -> None:
    async with odda_session() as h, dyn_server() as dyn:
        bid, tid = await h.open_browser()
        url = _set_cookie_url(
            dyn.tls_base,
            "cookie-clear-filter",
            "odda_a=1; Path=/",
            "odda_b=2; Path=/",
            "odda_c=3; Path=/",
        )
        await h.navigate(bid, tid, url)

        r = await h.call("cookies_clear", {"browser_id": bid, "name": "odda_b"})
        assert r == {"cleared": 1}

        ours = {
            c["name"]
            for c in await h.call("cookies_list", {"browser_id": bid})
            if c["domain"] == "127.0.0.1"
        }
        assert ours == {"odda_a", "odda_c"}


async def test_cookies_clear_wipes_whole_jar(odda_session) -> None:
    async with odda_session() as h, dyn_server() as dyn:
        bid, tid = await h.open_browser()
        url = _set_cookie_url(
            dyn.tls_base,
            "cookie-clear-all",
            "odda_a=1; HttpOnly; Path=/",
            "odda_b=2; Path=/",
        )
        await h.navigate(bid, tid, url)

        r = await h.call("cookies_clear", {"browser_id": bid})
        assert r["cleared"] >= 2

        jar = await h.call("cookies_list", {"browser_id": bid})
        assert not any(c["domain"] == "127.0.0.1" for c in jar)


async def test_cookies_set_plants_httponly_and_sends_on_wire(odda_session) -> None:
    async with odda_session() as h, dyn_server() as dyn:
        bid, tid = await h.open_browser()
        r = await h.call(
            "cookies_set",
            {
                "browser_id": bid,
                "cookies": [
                    {
                        "name": "odda_sess",
                        "value": "abc",
                        "domain": "127.0.0.1",
                        "path": "/",
                        "httpOnly": True,
                    },
                    {"name": "odda_pref", "value": "dark", "url": f"{dyn.tls_base}/"},
                ],
            },
        )
        assert r == {"set": 2}

        jar = {c["name"]: c for c in await h.call("cookies_list", {"browser_id": bid})}
        assert jar["odda_sess"]["value"] == "abc"
        assert jar["odda_sess"]["httpOnly"] is True
        assert jar["odda_pref"]["value"] == "dark"

        await h.navigate(bid, tid, f"{dyn.tls_base}/?marker=cookie-set-send")
        flow = await h.wait_flow("cookie-set-send")
        request = (h.data_dir / "flows" / flow["id"] / "request").read_bytes()
        assert b"odda_sess=abc" in request

        visible = await h.eval(bid, tid, "document.cookie")
        assert "odda_sess=" not in visible
        assert "odda_pref=dark" in visible


async def test_cookies_set_validates_and_unknown_browser_errors(odda_session) -> None:
    async with odda_session() as h:
        err = await h.call_error(
            "cookies_set",
            {"browser_id": "aaaaa", "cookies": [{"name": "odda_x"}]},
        )
        assert "name" in err and "value" in err

        err = await h.call_error(
            "cookies_set",
            {"browser_id": "aaaaa", "cookies": [{"name": "odda_x", "value": "1"}]},
        )
        assert "url" in err and "domain" in err

        err = await h.call_error(
            "cookies_set",
            {
                "browser_id": "aaaaa",
                "cookies": [
                    {
                        "name": "odda_x",
                        "value": "1",
                        "url": "http://127.0.0.1/",
                        "domain": "127.0.0.1",
                    }
                ],
            },
        )
        assert "not both" in err

        err = await h.call_error("cookies_list", {"browser_id": "zzzzz"})
        assert err == "Browser zzzzz not found."
