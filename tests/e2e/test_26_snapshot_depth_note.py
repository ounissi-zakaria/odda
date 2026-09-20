"""Snapshot depth capping: a capped snapshot announces its cap.

``depth`` caps the rendered tree: nodes below the cap are walked and
ref-stamped but render as childless boundary lines indistinguishable
from genuine leaves — the result looks complete when it is not. When
``depth`` is set and the tree actually reaches the cap, page_snapshot
now (1) tags each boundary line with its hidden subtree depth
(``[deeper=k]``) and (2) appends a trailing note with the tree's real
depth and the hidden line count. The facts come from one uncapped
server-side render whose text is discarded; nothing extra reaches the
agent but the tags and the note. Trees that fit under the cap stay
byte-clean (no probe, no noise); if the probe drifts (page mutated
between renders) the result degrades to silence, never to an error.

The pure seam (``_max_content_indent`` / ``_annotate_capped_snapshot``)
is tested directly below; the browser-level cases run the real
page_snapshot against the fixture page.
"""

from __future__ import annotations

import re

from odda.browser import _annotate_capped_snapshot, _max_content_depth
from tests.e2e.conftest import fixture_site

# Real captures (fixture page main > generic > list > listitem > button).
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
    """Prop lines render one level past their node even at the cap, so the
    gate counts only node/text lines — otherwise every capped tree with
    at-cap props would look 'reaching the cap'."""
    assert _max_content_depth(CAPPED.splitlines()) == 2
    assert _max_content_depth(FULL.splitlines()) == 4
    assert _max_content_depth("- main [ref=e2]:\n  - /url: #".splitlines()) == 0


def test_capped_tree_gets_boundary_tag_and_note() -> None:
    """Boundary line gets [deeper=k] (subtree levels hidden below the cap);
    note carries the cap, the real depth, and the hidden line count."""
    out = _annotate_capped_snapshot(CAPPED, FULL, depth=2)
    lines = out.splitlines()
    assert len(lines) == len(CAPPED.splitlines()) + 2  # blank + note
    assert lines[:-2] == [
        l.replace("- list [ref=e9]", "- list [ref=e9] [deeper=2]")
        for l in CAPPED.splitlines()
    ]
    assert lines[-2] == ""  # blank line before the note
    assert lines[-1] == (
        "[snapshot capped at depth 2 — tree is 4 levels deep, "
        "4 lines hidden; raise depth to see everything, "
        "or page_find to search without the full tree]"
    )


def test_boundary_with_props_keeps_colon() -> None:
    """A boundary node with props renders ``- key:`` + prop lines in the
    capped tree; the tag slots in before the YAML colon."""
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
    """Deepest nodes sit exactly at the cap but have no children: no
    boundary, no note — the probe resolved a false alarm."""
    assert _annotate_capped_snapshot(CAPPED, CAPPED, depth=2) == CAPPED


def test_drift_degrades_to_silence() -> None:
    """If the page mutated between the capped and uncapped renders and a
    capped line has no counterpart, return the capped tree untouched."""
    drifted = FULL.replace("- list [ref=e9]:", "- list [ref=e99]:")
    assert _annotate_capped_snapshot(CAPPED, drifted, depth=2) == CAPPED


def test_box_tags_align_across_renders() -> None:
    """With boxes=true the capped lines carry [box=...] but the probe
    render doesn't (and rect values may jitter): alignment ignores box
    tags, the tag lands after them, the note still fires."""
    capped_boxed = "\n".join(
        l + " [box=8,21,1264,18]" if l else l for l in CAPPED.splitlines()
    )
    out = _annotate_capped_snapshot(capped_boxed, FULL, depth=2)
    assert re.search(r"- list \[ref=e9\] \[box=[^\]]*\] \[deeper=2\]", out)
    assert "[snapshot capped at depth 2 —" in out


# --- real surface ---------------------------------------------------


def _note_re(depth: int) -> re.Pattern[str]:
    """The cap note for ``depth``; real tree depth varies (a focused body
    renders as an extra wrapper level), so only the cap is pinned."""
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
    """A cap two levels above the deepest node tags the list line with its
    hidden subtree depth and ends with the cap note stating the real depth."""
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
        # list sits exactly two levels above the buttons: boundary, 2 hidden
        assert re.search(r"- list \[ref=e\d+\] \[deeper=2\]", out)
        assert _note_re(real - 2).search(out)
        assert lines[-2] == ""  # blank line before the note


async def test_depth_fitting_tree_stays_clean(odda_session) -> None:
    """A cap above the tree (real + 5) and an uncapped call never
    annotate — no probe noise on trees the cap doesn't bite."""
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
    """A cap exactly at the tree's deepest level makes every boundary
    line a genuine leaf: the probe runs, finds nothing hidden, and the
    result stays clean rather than crying wolf."""
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
        assert '- button "Alpha" [ref=e' in out  # deepest level rendered


async def test_boxes_and_depth_compose(odda_session) -> None:
    """boxes=true with a capping depth: box tags on lines AND the cap
    note at the end — the annotation is orthogonal to geometry."""
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
    """Refs below the cap stay valid: learn Alpha's ref from a full
    snapshot, take a depth=2 snapshot (Alpha absent from the text), and
    click by the ref — the walk stamped it regardless of the cap."""
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
        assert "Alpha" not in capped  # hidden below the cap, ref still live
        assert _note_re(2).search(capped)
        await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": alpha_ref})
        assert (
            await h.eval(
                bid, tid, "document.getElementById('alpha-result').textContent"
            )
            == "clicked"
        )
