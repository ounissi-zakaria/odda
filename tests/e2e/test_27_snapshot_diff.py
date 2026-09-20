"""Snapshot diff mode: page_snapshot(diff=True) returns only what changed
since the tab's previous same-depth snapshot (ADR-0026).

The contract, each case a wire-level behavior an agent depends on:
- First diff (no baseline) returns the full tree announced by a note.
- Every page_snapshot stores its render as the baseline for its depth;
  a diff with nothing changed returns the one-line sentinel.
- A change comes back as `-`/`+` line pairs under an ancestor-path
  header, and the `+` lines' fresh refs are directly actionable.
- Any navigation clears the tab's baselines; the next diff re-announces.
- Depth defines the shape: different depths never cross-diff.
- Geometry never reads as change: boxes churn with scroll and are
  stripped before comparison.
- page_find renders internally and never touches baselines.
"""

from __future__ import annotations

import re

from tests.e2e.conftest import fixture_site

NO_CHANGES = "(no changes since previous snapshot)"
NO_BASELINE = "(no previous snapshot for this depth"


def _pluck_ref(text: str, needle: str) -> str:
    """Ref on the first line containing needle."""
    line = next(l for l in text.splitlines() if needle in l)
    m = re.search(r"\[ref=(e\d+)\]", line)
    assert m, f"no ref on line: {line!r}"
    return m.group(1)


async def test_first_diff_announces_full_tree_then_no_change_sentinel(
    odda_session,
) -> None:
    """diff=True with no baseline returns the whole tree announced by the
    note (and stores it); diffing again with nothing changed returns only
    the sentinel, not an empty result or a re-render."""
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
        # The tree is really there, not just the note.
        assert "Diff target" in first
        assert "alpha item" in first
        assert "Add item" in first

        second = await h.call(
            "page_snapshot", {"browser_id": bid, "tab_id": tid, "diff": True}
        )
        assert second == NO_CHANGES


async def test_diff_shows_change_and_plus_ref_is_actionable(odda_session) -> None:
    """After an action changes the tree, the diff carries only the change:
    ancestor-path header, -/+ markers, fresh refs on + lines — and a +
    ref drives page_click without any full re-snapshot."""
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
        # Unchanged content is never re-sent.
        assert "alpha item" not in diff
        assert "Rename heading" not in diff
        # The changed lines arrive marked, under an ancestor path header.
        assert diff.startswith("[1] ")
        plus = [l for l in diff.splitlines() if "gamma action" in l]
        assert plus, diff
        assert all(l.startswith("+ ") for l in plus)

        # The + ref is the fresh, actionable one.
        ref = _pluck_ref(diff, "gamma action")
        r = await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})
        assert r["status"] == "clicked"
        out = await h.eval(bid, tid, "document.getElementById('status').textContent")
        assert out == "gamma clicked"


async def test_diff_reports_removal_with_minus_lines(odda_session) -> None:
    """A removed element comes back as - lines carrying the old render's
    content, with no + counterpart."""
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
    """After navigating the same tab, a diff returns the announced full
    tree again instead of diffing across the page load."""
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
    """A depth-capped diff has its own baseline: the first depth=2 diff
    re-announces the full tree even though depth=None has one, and taking
    it leaves the depth=None baseline intact (its diff stays the
    sentinel)."""
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
    """boxes are viewport-relative and every scroll rewrites them; diff
    comparison strips geometry, so a scrolled boxes diff is still the
    sentinel — and the boxes param itself never breaks the diff."""
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
    """page_find takes a fresh snapshot internally but must not store it:
    after find observed the new tree, a diff still reports the change
    against the agent's last page_snapshot."""
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
    """An element whose name changed diffs as the old line removed and the
    new line added, both at the same position under one header."""
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
