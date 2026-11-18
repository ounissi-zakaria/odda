from __future__ import annotations

import re

from odda.browser import _annotate_capped_snapshot, _max_content_depth
from tests.e2e.conftest import fixture_site

CAPPED = """- main [ref=e2]:
  - heading "Docs" [level=1] [ref=e3]
  - navigation [ref=e4]:
    - link "Home" [ref=e5] [cursor=pointer]:
      - /url: "#"
    - link "About" [ref=e6] [cursor=pointer]:
      - /url: "#"
  - generic [ref=e7]:
    - heading "Topics" [level=2] [ref=e8]
    - list [ref=e9]"""

FULL = """- main [ref=e2]:
  - heading "Docs" [level=1] [ref=e3]
  - navigation [ref=e4]:
    - link "Home" [ref=e5] [cursor=pointer]:
      - /url: "#"
    - link "About" [ref=e6] [cursor=pointer]:
      - /url: "#"
  - generic [ref=e7]:
    - heading "Topics" [level=2] [ref=e8]
    - list [ref=e9]:
      - listitem [ref=e10]:
        - button "Alpha" [ref=e11]
      - listitem [ref=e12]:
        - button "Beta" [ref=e13]"""


def test_max_content_depth_ignores_prop_lines() -> None:
    assert _max_content_depth(CAPPED.splitlines()) == 2
    assert _max_content_depth(FULL.splitlines()) == 4
    assert _max_content_depth("- main [ref=e2]:\n  - /url: #".splitlines()) == 0


def test_capped_tree_gets_boundary_tag_and_note() -> None:
    out = _annotate_capped_snapshot(CAPPED, FULL, depth=2)
    lines = out.splitlines()
    assert len(lines) == len(CAPPED.splitlines()) + 2
    assert lines[:-2] == [
        l.replace("- list [ref=e9]", "- list [ref=e9] [deeper=2]")
        for l in CAPPED.splitlines()
    ]
    assert lines[-2] == ""
    assert lines[-1] == (
        "[snapshot capped at depth 2 — tree is 4 levels deep, "
        "4 lines hidden; raise depth to see everything, "
        "or page_find to search without the full tree]"
    )


def test_boundary_with_props_keeps_colon() -> None:
    capped = (
        "- main [ref=e2]:\n"
        '  - link "Docs" [ref=e5] [cursor=pointer]:\n'
        '    - /url: "/docs"'
    )
    full = (
        "- main [ref=e2]:\n"
        '  - link "Docs" [ref=e5] [cursor=pointer]:\n'
        '    - /url: "/docs"\n'
        '    - text: "Docs"\n'
        '    - text: " updated"'
    )
    out = _annotate_capped_snapshot(capped, full, depth=1)
    assert '  - link "Docs" [ref=e5] [cursor=pointer] [deeper=1]:' in out
    assert re.search(r"tree is 2 levels deep, 2 lines hidden", out)


def test_false_alarm_all_leaves_stays_clean() -> None:
    assert _annotate_capped_snapshot(CAPPED, CAPPED, depth=2) == CAPPED


def test_drift_degrades_to_silence() -> None:
    drifted = FULL.replace("- list [ref=e9]:", "- list [ref=e99]:")
    assert _annotate_capped_snapshot(CAPPED, drifted, depth=2) == CAPPED


def test_box_tags_align_across_renders() -> None:
    capped_boxed = "\n".join(
        l + " [box=8,21,1264,18]" if l else l for l in CAPPED.splitlines()
    )
    out = _annotate_capped_snapshot(capped_boxed, FULL, depth=2)
    assert re.search(r"- list \[ref=e9\] \[box=[^\]]*\] \[deeper=2\]", out)
    assert "[snapshot capped at depth 2 —" in out


def _note_re(depth: int) -> re.Pattern[str]:
    return re.compile(
        rf"\[snapshot capped at depth {depth} — tree is \d+ levels deep, "
        r"\d+ lines hidden; raise depth to see everything, "
        r"or page_find to search without the full tree\]\Z"
    )


async def _real_depth(h, bid: int, tid: int) -> int:
    full = await h.call("page_snapshot", {"browser_id": bid, "tab_id": tid})
    return _max_content_depth(full.splitlines())


def _ref_of(snapshot: str, text: str) -> str:
    line = next(l for l in snapshot.splitlines() if text in l)
    return re.search(r"\[ref=(e\d+)\]", line).group(1)


async def test_depth_capped_snapshot_announces_cap(odda_session) -> None:
    async with (
        odda_session() as h,
        fixture_site(["snapshot-depth.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/snapshot-depth.html")
        real = await _real_depth(h, bid, tid)
        out = await h.call(
            "page_snapshot",
            {"browser_id": bid, "tab_id": tid, "depth": real - 2},
        )
        lines = out.splitlines()
        assert re.search(r"- list \[ref=e\d+\] \[deeper=2\]", out)
        assert _note_re(real - 2).search(out)
        assert lines[-2] == ""


async def test_depth_fitting_tree_stays_clean(odda_session) -> None:
    async with (
        odda_session() as h,
        fixture_site(["snapshot-depth.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/snapshot-depth.html")
        real = await _real_depth(h, bid, tid)
        shallow = await h.call(
            "page_snapshot", {"browser_id": bid, "tab_id": tid, "depth": real + 5}
        )
        uncapped = await h.call("page_snapshot", {"browser_id": bid, "tab_id": tid})
        for out in (shallow, uncapped):
            assert "[deeper=" not in out
            assert "[snapshot capped" not in out


async def test_depth_false_alarm_leaves_at_cap_stay_clean(odda_session) -> None:
    async with (
        odda_session() as h,
        fixture_site(["snapshot-depth.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/snapshot-depth.html")
        real = await _real_depth(h, bid, tid)
        out = await h.call(
            "page_snapshot", {"browser_id": bid, "tab_id": tid, "depth": real}
        )
        assert "[deeper=" not in out
        assert "[snapshot capped" not in out
        assert '- button "Alpha" [ref=e' in out


async def test_boxes_and_depth_compose(odda_session) -> None:
    async with (
        odda_session() as h,
        fixture_site(["snapshot-depth.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/snapshot-depth.html")
        real = await _real_depth(h, bid, tid)
        out = await h.call(
            "page_snapshot",
            {"browser_id": bid, "tab_id": tid, "depth": real - 2, "boxes": True},
        )
        assert "[box=" in out
        assert _note_re(real - 2).search(out)


async def test_below_cap_ref_actionable_after_capped_snapshot(odda_session) -> None:
    async with (
        odda_session() as h,
        fixture_site(["snapshot-depth.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/snapshot-depth.html")
        full = await h.call("page_snapshot", {"browser_id": bid, "tab_id": tid})
        alpha_ref = _ref_of(full, 'button "Alpha"')
        capped = await h.call(
            "page_snapshot", {"browser_id": bid, "tab_id": tid, "depth": 2}
        )
        assert "Alpha" not in capped
        assert _note_re(2).search(capped)
        await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": alpha_ref})
        assert (
            await h.eval(
                bid, tid, "document.getElementById('alpha-result').textContent"
            )
            == "clicked"
        )
