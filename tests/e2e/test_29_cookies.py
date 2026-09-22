"""Cookie jar tools: browser-scoped read, delete, and set.

`cookies_list` reads the whole jar of one browser — including the
httpOnly cookies page JavaScript cannot see (the reason the tools exist:
`eval` on document.cookie is blind to them). `cookies_clear` deletes,
with exact-match name/domain/path filters or none for the whole jar.
`cookies_set` plants driver-shaped cookies, httpOnly included. All three
are browser-scoped: the jar is one per Browser record, not per tab.
"""

from __future__ import annotations

from urllib.parse import quote

from tests.e2e.conftest import dyn_server, odda_session


def _set_cookie_url(site: str, marker: str, *cookies: str) -> str:
    """Dyn-server URL whose response sets the given Set-Cookie headers.

    Each cookie is a raw ``Set-Cookie`` value (e.g.
    ``"odda_a=1; HttpOnly; Path=/"``); the helper URL-encodes it into a
    repeatable ``?header=set-cookie:`` query param.
    """
    encoded = "&".join(f"header=set-cookie:{quote(c, safe='')}" for c in cookies)
    return f"{site}/?marker={marker}&{encoded}"


async def test_cookies_list_sees_httponly_that_js_cannot(odda_session) -> None:
    """cookies_list returns the full jar — httpOnly cookies included,
    with their attributes — while document.cookie on the same page
    exposes only the non-httpOnly cookie."""
    async with odda_session() as h, dyn_server() as dyn:
        bid, tid = await h.open_browser()
        # Two Set-Cookie response headers: one httpOnly, one plain.
        # Same origin (127.0.0.1), Path=/.
        url = _set_cookie_url(
            dyn.tls_base,
            "cookie-httponly",
            "odda_ht=1; HttpOnly; Path=/",
            "odda_plain=2; Path=/",
        )
        await h.navigate(bid, tid, url)

        jar = {
            c["name"]: c for c in await h.call_json("cookies_list", {"browser_id": bid})
        }
        assert jar["odda_ht"]["value"] == "1"
        assert jar["odda_ht"]["httpOnly"] is True
        assert jar["odda_ht"]["domain"] == "127.0.0.1"
        assert jar["odda_plain"]["value"] == "2"
        assert jar["odda_plain"]["httpOnly"] is False

        # The page reads only the plain one — the tool is the only door
        # to the httpOnly cookie.
        visible = await h.eval(bid, tid, "document.cookie")
        assert visible == "odda_plain=2"


async def test_cookies_clear_name_filter_spares_siblings(odda_session) -> None:
    """cookies_clear with a name filter removes exactly the matching
    cookies and reports the count; same-domain siblings survive."""
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
            for c in await h.call_json("cookies_list", {"browser_id": bid})
            if c["domain"] == "127.0.0.1"
        }
        assert ours == {"odda_a", "odda_c"}


async def test_cookies_clear_wipes_whole_jar(odda_session) -> None:
    """cookies_clear with no filters empties the jar — httpOnly cookies
    included — and reports how many were removed."""
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
        assert r["cleared"] >= 2  # our two, plus any profile-seeded strays

        # The whole jar was wiped; assert on our origin — the base
        # profile on a dev machine can carry (or Chrome can re-set)
        # third-party cookies that are not ours to assert on.
        jar = await h.call_json("cookies_list", {"browser_id": bid})
        assert not any(c["domain"] == "127.0.0.1" for c in jar)


async def test_cookies_set_plants_httponly_and_sends_on_wire(odda_session) -> None:
    """cookies_set plants driver-shaped cookies — httpOnly included —
    into the jar; the page sees only the non-httpOnly ones, and the
    planted cookies go on the wire with the next navigation."""
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

        jar = {
            c["name"]: c for c in await h.call_json("cookies_list", {"browser_id": bid})
        }
        assert jar["odda_sess"]["value"] == "abc"
        assert jar["odda_sess"]["httpOnly"] is True
        assert jar["odda_pref"]["value"] == "dark"

        # The planted cookies go on the wire and the page sees only the
        # non-httpOnly one: navigate to the origin, read the captured
        # request bytes, then document.cookie from that page.
        await h.navigate(bid, tid, f"{dyn.tls_base}/?marker=cookie-set-send")
        flow = await h.wait_flow("cookie-set-send")
        request = (h.data_dir / "flows" / flow["id"] / "request").read_bytes()
        assert b"odda_sess=abc" in request

        visible = await h.eval(bid, tid, "document.cookie")
        assert "odda_sess=" not in visible
        assert "odda_pref=dark" in visible


async def test_cookies_set_validates_and_unknown_browser_errors(odda_session) -> None:
    """cookies_set rejects cookies missing value or scoping with
    named-field messages; every cookies_* tool rejects an unknown
    browser id with the house message."""
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
