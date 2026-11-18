from __future__ import annotations

import re

from tests.e2e.conftest import fixture_site


def _pluck_ref(text: str, needle: str, pattern: str = r"\[ref=(e\d+)\]") -> str:
    line = next(l for l in text.splitlines() if needle in l)
    m = re.search(pattern, line)
    assert m, f"no {pattern!r} on line: {line!r}"
    return m.group(1)


async def test_page_find_returns_match_with_context_and_path(odda_session) -> None:
    async with (
        odda_session() as h,
        fixture_site(["page-find.html", "page-find-inner.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-find.html")

        full = await h.page_snapshot(bid, tid)
        found = await h.call(
            "page_find", {"browser_id": bid, "tab_id": tid, "regex": "Create account"}
        )
        assert isinstance(found, str)
        assert "Create account" in found
        assert found.startswith("[1] ")
        assert "Signup form" in found.splitlines()[0]
        assert "Deep username" in found
        assert "Filler paragraph one" in found
        assert len(found.splitlines()) < len(full.splitlines()) // 2
        assert "Pricing overview" not in found

        ref = _pluck_ref(found, "Create account")
        r = await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})
        assert r["status"] == "clicked"
        out = await h.eval(
            bid, tid, "document.getElementById('click-result').textContent"
        )
        assert out == "click result: account created"


async def test_page_find_same_name_matches_get_distinct_paths(odda_session) -> None:
    async with (
        odda_session() as h,
        fixture_site(["page-find.html", "page-find-inner.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-find.html")

        found = await h.call(
            "page_find", {"browser_id": bid, "tab_id": tid, "regex": "Download report"}
        )
        assert isinstance(found, str)
        blocks = found.split("\n\n")
        assert len(blocks) == 2, f"expected 2 windows, got {len(blocks)}: {found!r}"
        firsts = [b.splitlines()[0] for b in blocks]
        assert firsts[0].startswith("[1] ") and firsts[1].startswith("[2] ")
        paths = " | ".join(firsts)
        assert "primary" in paths and "Downloads" in paths
        assert "navigation" in firsts[0]
        assert "link" in blocks[0] and "button" in blocks[1]


async def test_page_find_adjacent_matches_coalesce_into_one_window(
    odda_session,
) -> None:
    async with (
        odda_session() as h,
        fixture_site(["page-find.html", "page-find-inner.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-find.html")
        found = await h.call(
            "page_find", {"browser_id": bid, "tab_id": tid, "regex": "[Ee]mail"}
        )
        assert isinstance(found, str)
        blocks = found.split("\n\n")
        assert len(blocks) == 1, f"adjacent matches must coalesce: {found!r}"
        assert "Email address" in found and "Email confirmation" in found


async def test_page_find_regex_semantics_and_no_match(odda_session) -> None:
    async with (
        odda_session() as h,
        fixture_site(["page-find.html", "page-find-inner.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-find.html")

        strict = await h.call(
            "page_find", {"browser_id": bid, "tab_id": tid, "regex": "EMAIL"}
        )
        assert strict == "No matches for /EMAIL/ in snapshot"

        insensitive = await h.call(
            "page_find", {"browser_id": bid, "tab_id": tid, "regex": "(?i)email"}
        )
        assert "Email address" in insensitive


async def test_page_find_reaches_into_iframes(odda_session) -> None:
    async with (
        odda_session() as h,
        fixture_site(["page-find.html", "page-find-inner.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-find.html")
        await h.wait_for(
            bid,
            tid,
            "document.getElementById('find-inner-frame')"
            ".contentWindow.__oddaPageFindIframeLoaded === true",
        )
        found = await h.call(
            "page_find", {"browser_id": bid, "tab_id": tid, "regex": "Iframe target"}
        )
        assert isinstance(found, str)
        iref = _pluck_ref(found, "Iframe target", r"\[ref=(f\d+e\d+)\]")
        r = await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": iref})
        assert r["status"] == "clicked" and r["ref"] == iref
        out = await h.eval(
            bid,
            tid,
            "document.getElementById('find-inner-frame')"
            ".contentWindow.document.getElementById('iframe-result')"
            ".textContent",
        )
        assert out == "iframe result: clicked"


async def test_page_snapshot_depth_caps_render_but_keeps_refs_alive(
    odda_session,
) -> None:
    async with (
        odda_session() as h,
        fixture_site(["page-find.html", "page-find-inner.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-find.html")

        full = await h.page_snapshot(bid, tid)
        deep_line = next(
            l for l in full.splitlines() if "Deep username" in l and "[ref=" in l
        )
        deep_ref = re.search(r"\[ref=(e\d+)\]", deep_line).group(1)

        shallow = await h.call(
            "page_snapshot", {"browser_id": bid, "tab_id": tid, "depth": 1}
        )
        assert isinstance(shallow, str)
        assert shallow
        assert "Deep username" not in shallow

        await h.call(
            "page_fill",
            {"browser_id": bid, "tab_id": tid, "ref": deep_ref, "value": "alice"},
        )
        out = await h.eval(bid, tid, "document.getElementById('deep-username').value")
        assert out == "alice"


async def test_page_find_boxes_feed_a_coordinate_click(odda_session) -> None:
    async with (
        odda_session() as h,
        fixture_site(["page-find.html", "page-find-inner.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-find.html")
        found = await h.call(
            "page_find",
            {"browser_id": bid, "tab_id": tid, "regex": "Fixed target", "boxes": True},
        )
        assert isinstance(found, str)
        line = next(l for l in found.splitlines() if "Fixed target" in l)
        m = re.search(
            r"\[box=(\d+(?:\.\d+)?),(\d+(?:\.\d+)?),(\d+(?:\.\d+)?),(\d+(?:\.\d+)?)\]",
            line,
        )
        assert m, f"no [box=...] on match line: {line!r}"
        x, y, w, hgt = map(float, m.groups())
        assert (w, hgt) == (100.0, 50.0)
        cx, cy = x + w / 2, y + hgt / 2
        r = await h.call(
            "page_click", {"browser_id": bid, "tab_id": tid, "x": cx, "y": cy}
        )
        assert r["status"] == "clicked"
        out = await h.eval(
            bid, tid, "document.getElementById('box-result').textContent"
        )
        assert out == "box result: clicked"

        plain = await h.call(
            "page_find", {"browser_id": bid, "tab_id": tid, "regex": "Fixed target"}
        )
        assert "[box=" not in plain


async def test_page_find_invalid_regex_errors_cleanly(odda_session) -> None:
    async with (
        odda_session() as h,
        fixture_site(["page-find.html", "page-find-inner.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-find.html")
        err = await h.call_error(
            "page_find", {"browser_id": bid, "tab_id": tid, "regex": "([unclosed"}
        )
        assert err.startswith("Invalid regex:")
