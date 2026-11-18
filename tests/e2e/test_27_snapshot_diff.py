# See ADR-0026.
from __future__ import annotations

import re

from tests.e2e.conftest import fixture_site

NO_CHANGES = "(no changes since previous snapshot)"
NO_BASELINE = "(no previous snapshot for this depth"


def _pluck_ref(text: str, needle: str) -> str:
    line = next(l for l in text.splitlines() if needle in l)
    m = re.search(r"\[ref=(e\d+)\]", line)
    assert m, f"no ref on line: {line!r}"
    return m.group(1)


async def test_first_diff_announces_full_tree_then_no_change_sentinel(
    odda_session,
) -> None:
    async with (
        odda_session() as h,
        fixture_site(["page-snapshot-diff.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-snapshot-diff.html")

        first = await h.call(
            "page_snapshot", {"browser_id": bid, "tab_id": tid, "diff": True}
        )
        assert isinstance(first, str)
        assert first.startswith(NO_BASELINE), first.splitlines()[0]
        assert "Diff target" in first
        assert "alpha item" in first
        assert "Add item" in first

        second = await h.call(
            "page_snapshot", {"browser_id": bid, "tab_id": tid, "diff": True}
        )
        assert second == NO_CHANGES


async def test_diff_shows_change_and_plus_ref_is_actionable(odda_session) -> None:
    async with (
        odda_session() as h,
        fixture_site(["page-snapshot-diff.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-snapshot-diff.html")

        full = await h.page_snapshot(bid, tid)
        add_ref = _pluck_ref(full, "Add item")
        r = await h.call(
            "page_click", {"browser_id": bid, "tab_id": tid, "ref": add_ref}
        )
        assert r["status"] == "clicked"

        diff = await h.call(
            "page_snapshot", {"browser_id": bid, "tab_id": tid, "diff": True}
        )
        assert isinstance(diff, str)
        assert diff != NO_CHANGES
        assert "alpha item" not in diff
        assert "Rename heading" not in diff
        assert diff.startswith("[1] ")
        plus = [l for l in diff.splitlines() if "gamma action" in l]
        assert plus, diff
        assert all(l.startswith("+ ") for l in plus)

        ref = _pluck_ref(diff, "gamma action")
        r = await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})
        assert r["status"] == "clicked"
        out = await h.eval(bid, tid, "document.getElementById('status').textContent")
        assert out == "gamma clicked"


async def test_diff_reports_removal_with_minus_lines(odda_session) -> None:
    async with (
        odda_session() as h,
        fixture_site(["page-snapshot-diff.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-snapshot-diff.html")

        full = await h.page_snapshot(bid, tid)
        ref = _pluck_ref(full, "Remove first item")
        await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})

        diff = await h.call(
            "page_snapshot", {"browser_id": bid, "tab_id": tid, "diff": True}
        )
        assert isinstance(diff, str)
        minus = [l for l in diff.splitlines() if "alpha item" in l]
        assert minus and all(l.startswith("- ") for l in minus), diff
        assert not [l for l in diff.splitlines() if "gamma" in l]


async def test_navigation_wipes_baseline(odda_session) -> None:
    async with (
        odda_session() as h,
        fixture_site(["page-snapshot-diff.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-snapshot-diff.html")

        await h.page_snapshot(bid, tid)
        await h.navigate(bid, tid, f"{fx.base}/page-snapshot-diff.html")
        diff = await h.call(
            "page_snapshot", {"browser_id": bid, "tab_id": tid, "diff": True}
        )
        assert isinstance(diff, str)
        assert diff.startswith(NO_BASELINE), diff.splitlines()[0]
        assert "alpha item" in diff


async def test_depth_shapes_never_cross_diff(odda_session) -> None:
    async with (
        odda_session() as h,
        fixture_site(["page-snapshot-diff.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-snapshot-diff.html")

        await h.page_snapshot(bid, tid)

        capped = await h.call(
            "page_snapshot",
            {"browser_id": bid, "tab_id": tid, "depth": 2, "diff": True},
        )
        assert isinstance(capped, str)
        assert capped.startswith(NO_BASELINE), capped.splitlines()[0]

        second = await h.call(
            "page_snapshot",
            {"browser_id": bid, "tab_id": tid, "depth": 2, "diff": True},
        )
        assert second == NO_CHANGES

        plain = await h.call(
            "page_snapshot", {"browser_id": bid, "tab_id": tid, "diff": True}
        )
        assert plain == NO_CHANGES


async def test_scroll_churns_boxes_but_never_reads_as_change(odda_session) -> None:
    async with (
        odda_session() as h,
        fixture_site(["page-snapshot-diff.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-snapshot-diff.html")

        await h.page_snapshot(bid, tid)
        await h.eval(bid, tid, "window.scrollBy(0, 300)")

        boxed = await h.call(
            "page_snapshot",
            {"browser_id": bid, "tab_id": tid, "diff": True, "boxes": True},
        )
        assert boxed == NO_CHANGES
        plain = await h.call(
            "page_snapshot", {"browser_id": bid, "tab_id": tid, "diff": True}
        )
        assert plain == NO_CHANGES


async def test_page_find_never_touches_the_baseline(odda_session) -> None:
    async with (
        odda_session() as h,
        fixture_site(["page-snapshot-diff.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-snapshot-diff.html")

        full = await h.page_snapshot(bid, tid)
        ref = _pluck_ref(full, "Add item")
        await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})

        found = await h.call(
            "page_find", {"browser_id": bid, "tab_id": tid, "regex": "gamma action"}
        )
        assert "gamma action" in found

        diff = await h.call(
            "page_snapshot", {"browser_id": bid, "tab_id": tid, "diff": True}
        )
        assert isinstance(diff, str)
        assert diff != NO_CHANGES
        assert "gamma action" in diff


async def test_rename_is_a_minus_plus_pair(odda_session) -> None:
    async with (
        odda_session() as h,
        fixture_site(["page-snapshot-diff.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-snapshot-diff.html")

        full = await h.page_snapshot(bid, tid)
        ref = _pluck_ref(full, "Rename heading")
        await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})

        diff = await h.call(
            "page_snapshot", {"browser_id": bid, "tab_id": tid, "diff": True}
        )
        assert isinstance(diff, str)
        minus = [l for l in diff.splitlines() if "Diff target" in l]
        plus = [l for l in diff.splitlines() if "Renamed heading" in l]
        assert minus and all(l.startswith("- ") for l in minus), diff
        assert plus and all(l.startswith("+ ") for l in plus), diff
