"""Page find + scoped/deep/boxed snapshots: locating targets without
the full tree.

page_find searches a fresh AI-mode snapshot with a regex and returns
matched lines with context and the ancestor path from the tree root —
not the whole tree (upstream browser_find shape, grep -C 3 semantics,
regex-only per the plan). page_snapshot grows ``target`` (a snapshot
ref, or any Playwright selector so a subtree can be snapshotted with
no prior full snapshot), ``depth`` (boundary nodes render childless),
and ``boxes`` ([box=x,y,w,h] per line — the Ref -> Coordinate bridge:
find a box, click its center). The bad-argument contracts (invalid
regex, unresolvable target) error cleanly with odda messages verbatim.
"""

from __future__ import annotations

import re

from tests.e2e.conftest import fixture_site


def _pluck_ref(text: str, needle: str, pattern: str = r"\[ref=(e\d+)\]") -> str:
    """First line containing needle; pluck its (frame-prefixed) ref."""
    line = next(l for l in text.splitlines() if needle in l)
    m = re.search(pattern, line)
    assert m, f"no {pattern!r} on line: {line!r}"
    return m.group(1)


async def test_page_find_returns_match_with_context_and_path(odda_session) -> None:
    """A find match comes back with its ref, surrounding context lines,
    and the ancestor path from the root — and its ref clicks."""
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
        # The match, with its ref and its ancestor path from the root
        # (verified shape: "[1] generic [active] [ref=e1] > main [ref=e8]
        # > form "Signup form" [ref=e9]" then the windowed lines).
        assert "Create account" in found
        assert found.startswith("[1] ")
        assert "Signup form" in found.splitlines()[0]
        # Context around the match (3 lines each side), but far from
        # the whole tree.
        assert "Deep username" in found  # sibling context above
        assert "Filler paragraph one" in found  # context below
        assert len(found.splitlines()) < len(full.splitlines()) // 2
        assert "Pricing overview" not in found

        # The discovered ref drives a real click (find -> ref -> action).
        ref = _pluck_ref(found, "Create account")
        r = await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})
        assert r["status"] == "clicked"
        out = await h.eval(
            bid, tid, "document.getElementById('click-result').textContent"
        )
        assert out == "click result: account created"


async def test_page_find_same_name_matches_get_distinct_paths(odda_session) -> None:
    """Two 'Download report' elements (nav link, section button) match in
    two windows, each labeled with its own root-to-match ancestor path."""
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
        # Same accessible name, different locations: the paths tell them
        # apart (nav link under banner/navigation, button under Downloads).
        paths = " | ".join(firsts)
        assert "primary" in paths and "Downloads" in paths
        assert "navigation" in firsts[0]
        assert "link" in blocks[0] and "button" in blocks[1]


async def test_page_find_adjacent_matches_coalesce_into_one_window(
    odda_session,
) -> None:
    """Matches closer than 2*context lines merge into a single window."""
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
    """Matching is Python re per line, case-sensitive by default ((?i)
    opts in); a regex with no hits is a clean no-match result, not an
    error."""
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
    """A match inside an iframe carries a frame-prefixed ref that
    clicks in the iframe."""
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


async def test_page_snapshot_scoped_by_target_and_limited_by_depth(
    odda_session,
) -> None:
    """target scopes the snapshot to one subtree — by selector (no prior
    snapshot needed) or by ref — and depth caps how deep it renders."""
    async with (
        odda_session() as h,
        fixture_site(["page-find.html", "page-find-inner.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-find.html")

        # Selector target: the signup form subtree, without the banner.
        form = await h.call(
            "page_snapshot",
            {"browser_id": bid, "tab_id": tid, "target": "#signup"},
        )
        assert isinstance(form, str)
        assert "Signup form" in form
        assert "Create account" in form
        assert "Deep username" in form
        assert "Pricing overview" not in form

        # Depth cap: boundary node present, deep leaf gone.
        shallow = await h.call(
            "page_snapshot",
            {"browser_id": bid, "tab_id": tid, "target": "#signup", "depth": 1},
        )
        assert isinstance(shallow, str)
        assert "Create account" in shallow or "Email address" in shallow
        assert "Deep username" not in shallow

        # Ref target: same subtree scoping via a ref from a full snapshot
        # (the first "Download report" in document order is the nav link).
        full = await h.page_snapshot(bid, tid)
        ref = _pluck_ref(full, "Download report", r"\[ref=(e\d+)\]")
        leaf = await h.call(
            "page_snapshot", {"browser_id": bid, "tab_id": tid, "target": ref}
        )
        assert isinstance(leaf, str)
        assert "Download report" in leaf
        assert "Pricing overview" not in leaf
        assert "Open archive" not in leaf  # other match's subtree


async def test_page_find_boxes_feed_a_coordinate_click(odda_session) -> None:
    """boxes=True puts [box=x,y,width,height] on find lines; the box
    center is a valid page_click coordinate (Ref -> Coordinate bridge)."""
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
        # The box on the MATCHED line (not an ancestor's box from the
        # path line): fixed-target div is 100x50, unlike any ancestor.
        line = next(l for l in found.splitlines() if "Fixed target" in l)
        m = re.search(
            r"\[box=(\d+(?:\.\d+)?),(\d+(?:\.\d+)?),(\d+(?:\.\d+)?),(\d+(?:\.\d+)?)\]",
            line,
        )
        assert m, f"no [box=...] on match line: {line!r}"
        x, y, w, hgt = map(float, m.groups())
        assert (w, hgt) == (100.0, 50.0)  # the fixed div's size
        cx, cy = x + w / 2, y + hgt / 2
        r = await h.call(
            "page_click", {"browser_id": bid, "tab_id": tid, "x": cx, "y": cy}
        )
        assert r["status"] == "clicked"
        out = await h.eval(
            bid, tid, "document.getElementById('box-result').textContent"
        )
        assert out == "box result: clicked"

        # Without boxes, geometry is absent.
        plain = await h.call(
            "page_find", {"browser_id": bid, "tab_id": tid, "regex": "Fixed target"}
        )
        assert "[box=" not in plain


async def test_page_find_invalid_regex_errors_cleanly(odda_session) -> None:
    """An unparseable regex is a param error naming the regex problem —
    before any page is touched."""
    async with (
        odda_session() as h,
        fixture_site(["page-find.html", "page-find-inner.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-find.html")
        err = await h.call_error(
            "page_find", {"browser_id": bid, "tab_id": tid, "regex": "([unclosed"}
        )
        assert err.startswith("Invalid regex:")


async def test_page_snapshot_unresolvable_target_errors_cleanly(odda_session) -> None:
    """A target that resolves to nothing (stale ref shape, unknown
    selector) errors with the snapshot error message, not a crash."""
    async with (
        odda_session() as h,
        fixture_site(["page-find.html", "page-find-inner.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-find.html")
        err = await h.call_error(
            "page_snapshot", {"browser_id": bid, "tab_id": tid, "target": "e99999"}
        )
        assert err.startswith("Snapshot error:")

        err = await h.call_error(
            "page_snapshot",
            {"browser_id": bid, "tab_id": tid, "target": ".does-not-exist"},
        )
        assert err.startswith("Snapshot error:")
