"""Page interaction: snapshot, click, fill, hover, upload.

Port of scrut 12-page-interaction.md. The snapshot is text (a
YAML-ish a11y tree) with ``[ref=eN]`` tags (``[ref=f<frameSeq>eN]``
inside iframes). Refs drive click/fill/hover/upload; click/hover
also accept viewport coordinates as a raw trusted event. ``fill``
``value`` xor ``file`` is back on the MCP surface (file reads the
payload from a server-side path); the behavioral contracts (event
types, clears-first, stale-ref timing, iframe refs) are the ones
asserted here.
"""

from __future__ import annotations

import json
import re
from io import BytesIO
from pathlib import Path

from PIL import Image

from tests.e2e.conftest import fixture_site


def _pluck_ref(snapshot: str, text: str, pattern: str = r"\[ref=(e\d+)\]") -> str:
    """Find the line containing text and pluck its ref (eN or fN eN)."""
    line = next(l for l in snapshot.splitlines() if text in l)
    m = re.search(pattern, line)
    assert m, f"no {pattern!r} on line: {line!r}"
    return m.group(1)


async def test_page_snapshot_and_ref_clicks(odda_session) -> None:
    """Snapshot shows refs (top + iframe); clicking by ref fires the
    handler in the top frame and inside the iframe."""
    async with (
        odda_session() as h,
        fixture_site(["page-interaction.html", "page-interaction-inner.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-interaction.html")
        # Wait for the iframe to load so the snapshot can reach into it.
        await h.wait_for(
            bid,
            tid,
            "document.getElementById('inner-frame')"
            ".contentWindow.__oddaPageIframeLoaded === true",
        )

        snap = await h.page_snapshot(bid, tid)
        assert isinstance(snap, str) and "[ref=e" in snap
        assert "Click me" in snap
        assert "textbox" in snap.lower() or "text" in snap.lower()
        assert re.search(r"\[ref=f\d+e\d+\]", snap)
        assert "Iframe button" in snap

        # Click the top-frame button by ref.
        ref = _pluck_ref(snap, "Click me")
        r = await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})
        assert r["status"] == "clicked" and r["ref"] == ref
        out = await h.eval(
            bid, tid, "document.getElementById('click-result').textContent"
        )
        assert out == "clicked"

        # Click an iframe ref: the handler inside the iframe fires.
        iref = _pluck_ref(snap, "Iframe button", r"\[ref=(f\d+e\d+)\]")
        r = await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": iref})
        assert r["status"] == "clicked" and r["ref"] == iref
        out = await h.eval(
            bid,
            tid,
            "document.getElementById('inner-frame').contentWindow"
            ".document.getElementById('iframe-result').textContent",
        )
        assert out == "clicked in iframe"


async def test_page_coordinate_click_and_hover(odda_session) -> None:
    """Coord clicks/hovers dispatch raw trusted events with no element
    resolution; empty space is a success no-op; iframes are reached."""
    async with (
        odda_session() as h,
        fixture_site(["page-interaction.html", "page-interaction-inner.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-interaction.html")
        await h.wait_for(
            bid,
            tid,
            "document.getElementById('inner-frame')"
            ".contentWindow.__oddaPageIframeLoaded === true",
        )

        # coord-click-target is position:fixed at 50,300 with size
        # 100x60 → its center (100, 330) is a stable viewport point.
        r = await h.call(
            "page_click", {"browser_id": bid, "tab_id": tid, "x": 100, "y": 330}
        )
        assert r["status"] == "clicked"
        assert (r["x"], r["y"]) == (100, 330)
        out = await h.eval(
            bid, tid, "document.getElementById('coord-click-result').textContent"
        )
        assert out == "coord clicked @ 100,330"

        # coord-hover-target: fixed at 200,300 → center (250, 330).
        r = await h.call(
            "page_hover", {"browser_id": bid, "tab_id": tid, "x": 250, "y": 330}
        )
        assert r["status"] == "hovered"
        assert (r["x"], r["y"]) == (250, 330)
        out = await h.eval(
            bid, tid, "document.getElementById('coord-hover-result').textContent"
        )
        assert out == "coord hovered @ 250,330"

        # A coord click lands on whatever renders at that point — the
        # iframe's fixed button included. Discover its viewport point
        # via getBoundingClientRect (the real agent workflow).
        xy = await h.eval(
            bid,
            tid,
            "(()=>{const r=document.getElementById('inner-frame')"
            ".getBoundingClientRect();const b=document.getElementById("
            "'inner-frame').contentWindow.document.getElementById("
            "'iframe-coord-btn').getBoundingClientRect();"
            "return JSON.stringify("
            "[r.left+b.left+b.width/2, r.top+b.top+b.height/2])})()",
        )
        x, y = json.loads(xy)
        r = await h.call(
            "page_click", {"browser_id": bid, "tab_id": tid, "x": x, "y": y}
        )
        assert r["status"] == "clicked"
        out = await h.eval(
            bid,
            tid,
            "document.getElementById('inner-frame').contentWindow"
            ".document.getElementById('iframe-result').textContent",
        )
        # The iframe handler records iframe-relative dispatch coords.
        assert re.fullmatch(r"coord in iframe @ 5\d*,11\d*", out), out

        # Empty space: no element resolution means no failure; the
        # dispatch lands on the body and fires nothing.
        r = await h.call(
            "page_click", {"browser_id": bid, "tab_id": tid, "x": 700, "y": 10}
        )
        assert r["status"] == "clicked"
        out = await h.eval(
            bid,
            tid,
            "JSON.stringify(["
            "document.getElementById('coord-click-result').textContent, "
            "document.getElementById('coord-hover-result').textContent])",
        )
        assert json.loads(out) == [
            "coord clicked @ 100,330",
            "coord hovered @ 250,330",
        ]


async def test_page_targeting_argument_errors(odda_session) -> None:
    """ref+coords, x without y, and no targeting at all all error before
    any browser interaction, with param-name messages."""
    async with (
        odda_session() as h,
        fixture_site(["page-interaction.html", "page-interaction-inner.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-interaction.html")

        err = await h.call_error(
            "page_click",
            {"browser_id": bid, "tab_id": tid, "ref": "e1", "x": 10, "y": 10},
        )
        assert err == "Provide either x/y or ref, not both"
        err = await h.call_error(
            "page_click", {"browser_id": bid, "tab_id": tid, "x": 10}
        )
        assert err == "Provide both x and y for coordinate mode"
        err = await h.call_error("page_hover", {"browser_id": bid, "tab_id": tid})
        assert err == "Provide either ref or x/y coordinates"


async def test_page_fill_hover_upload_and_stale_ref(
    odda_session, tmp_path: Path
) -> None:
    """fill fires input and clears first; a multiline value survives
    verbatim; hover fires mouseenter; upload needs an aria-label eval
    first; a stale ref errors within the requested timeout."""
    async with (
        odda_session() as h,
        fixture_site(["page-interaction.html", "page-interaction-inner.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/page-interaction.html")
        await h.wait_for(
            bid,
            tid,
            "document.getElementById('inner-frame')"
            ".contentWindow.__oddaPageIframeLoaded === true",
        )
        snap = await h.page_snapshot(bid, tid)

        # Fill the textbox: input handler echoes the value.
        tref = _pluck_ref(snap, "Type here")
        r = await h.call(
            "page_fill",
            {"browser_id": bid, "tab_id": tid, "ref": tref, "value": "hello"},
        )
        assert r["status"] == "filled" and r["ref"] == tref
        out = await h.eval(
            bid, tid, "document.getElementById('text-result').textContent"
        )
        assert out == "hello"

        # Fill clears first: "world" replaces "hello" (input value read
        # back directly, since the handler would echo either way).
        await h.call(
            "page_fill",
            {"browser_id": bid, "tab_id": tid, "ref": tref, "value": "world"},
        )
        out = await h.eval(bid, tid, "document.getElementById('text-input').value")
        assert out == "world"

        # Multiline payload from a file (value xor file is back): the
        # file's exact bytes, newlines included, land in the textarea.
        aref = _pluck_ref(snap, "Paste here")
        payload = "<b>line1</b>\n<i>line2</i>\n<p>line3</p>"
        payload_file = tmp_path / "fill-payload.html"
        payload_file.write_text(payload)
        r = await h.call(
            "page_fill",
            {
                "browser_id": bid,
                "tab_id": tid,
                "ref": aref,
                "file": str(payload_file),
            },
        )
        assert r["status"] == "filled" and r["ref"] == aref
        out = await h.eval(
            bid, tid, "JSON.stringify(document.getElementById('area-input').value)"
        )
        assert json.loads(out) == payload

        # Both / neither / missing file: param-name messages.
        err = await h.call_error(
            "page_fill",
            {
                "browser_id": bid,
                "tab_id": tid,
                "ref": aref,
                "value": "x",
                "file": str(payload_file),
            },
        )
        assert err == "Provide either value or file, not both"
        err = await h.call_error(
            "page_fill", {"browser_id": bid, "tab_id": tid, "ref": aref}
        )
        assert err == "Provide value <string> or file <path>"
        err = await h.call_error(
            "page_fill",
            {
                "browser_id": bid,
                "tab_id": tid,
                "ref": aref,
                "file": str(tmp_path / "no-such.html"),
            },
        )
        assert err.startswith("File not found:")

        # Hover the ref: mouseenter fires.
        href = _pluck_ref(snap, "Hover me")
        r = await h.call("page_hover", {"browser_id": bid, "tab_id": tid, "ref": href})
        assert r["status"] == "hovered" and r["ref"] == href
        out = await h.eval(
            bid, tid, "document.getElementById('hover-result').textContent"
        )
        assert out == "hovered"

        # A nameless file input is invisible to the snapshot: eval an
        # aria-label onto it, re-snapshot, then upload.
        await h.eval(
            bid,
            tid,
            "document.getElementById('file-input')"
            ".setAttribute('aria-label', 'Upload files'); 'ok'",
        )
        snap = await h.page_snapshot(bid, tid)
        ufref = _pluck_ref(snap, "Upload files")
        up = tmp_path / "upload-test.txt"
        up.write_text("test file content\n")
        r = await h.call(
            "page_upload",
            {
                "browser_id": bid,
                "tab_id": tid,
                "ref": ufref,
                "files": [str(up)],
            },
        )
        assert r["status"] == "uploaded" and r["ref"] == ufref
        assert len(r["files"]) == 1 and "upload-test.txt" in r["files"][0]
        out = await h.eval(
            bid, tid, "document.getElementById('upload-result').textContent"
        )
        assert "upload-test.txt" in out

        # Stale ref: navigate away, then click the old ref — a clean
        # error within the requested 2s, not a Playwright 30s hang.
        await h.navigate(bid, tid, "about:blank")
        err = await h.call_error(
            "page_click",
            {"browser_id": bid, "tab_id": tid, "ref": href, "timeout": 2},
        )
        assert re.search(r"did not resolve or become actionable within", err)

        # Missing tab / browser errors.
        err = await h.call_error(
            "page_click", {"browser_id": bid, "tab_id": 9999, "ref": "e1"}
        )
        assert err == f"Tab 9999 not found in browser {bid}."
        err = await h.call_error(
            "page_fill",
            {"browser_id": 9999, "tab_id": tid, "ref": "e1", "value": "x"},
        )
        assert err == "Browser 9999 not found."
        err = await h.call_error("page_snapshot", {"browser_id": bid, "tab_id": 9999})
        assert err == f"Tab 9999 not found in browser {bid}."
        err = await h.call_error("page_snapshot", {"browser_id": 9999, "tab_id": tid})
        assert err == "Browser 9999 not found."


_MAGENTA_RADIUS = 4


def _is_magentaish(r: int, g: int, b: int) -> bool:
    """JPEG-lossy magenta: chroma subsampling bleeds the channels."""
    return r > 200 and b > 200 and g < 120


def _magenta_at(img: Image.Image, x: int, y: int) -> bool:
    """Any pixel within radius of (x, y) reads magenta."""
    px = img.load()
    for xx in range(x - _MAGENTA_RADIUS, x + _MAGENTA_RADIUS + 1):
        for yy in range(y - _MAGENTA_RADIUS, y + _MAGENTA_RADIUS + 1):
            if 0 <= xx < img.width and 0 <= yy < img.height:
                r, g, b = px[xx, yy]
                if _is_magentaish(r, g, b):
                    return True
    return False


def _has_chip(img: Image.Image, x: int, y: int) -> bool:
    """A filled label block near (x, y): a vertical magenta run >= 8px.
    The 2px box stroke cannot produce one."""
    px = img.load()
    for xx in range(max(0, x - 40), min(img.width, x + 40)):
        for yy in range(max(0, y - 30), min(img.height, y + 30)):
            if not _is_magentaish(*px[xx, yy]):
                continue
            run = 0
            for k in range(yy, min(img.height, yy + 12)):
                if _is_magentaish(*px[xx, k]):
                    run += 1
                else:
                    break
            if run >= 8:
                return True
    return False


async def test_screenshot_annotate_draws_ref_boxes(odda_session) -> None:
    """annotate=True draws each ref's box stroke at its known viewport
    position plus a filled label chip at the box's top-left; the flag
    defaults off and the plain result is unannotated."""
    body = (
        '<button id="go" style="position:absolute;left:100px;top:80px;'
        'width:120px;height:40px;">Go</button>'
        '<input id="q" aria-label="Query" style="position:absolute;'
        'left:300px;top:200px;width:200px;height:32px;">'
    )
    async with odda_session() as h, fixture_site(index_body=body) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        # Flag defaults off: the plain screenshot carries no annotation.
        plain = await h.call("screenshot", {"browser_id": bid, "tab_id": tid})
        assert str(plain).endswith(".jpeg")
        plain_img = Image.open(BytesIO(Path(plain).read_bytes())).convert("RGB")
        assert not _magenta_at(plain_img, 100, 80)

        shot = await h.call(
            "screenshot", {"browser_id": bid, "tab_id": tid, "annotate": True}
        )
        assert str(shot).endswith(".jpeg")
        img = Image.open(BytesIO(Path(shot).read_bytes())).convert("RGB")
        # Box strokes land at the elements' known viewport positions —
        # boxes are viewport CSS px scaled by DSF (1 in odda's launches).
        assert _magenta_at(img, 100, 80), "button box stroke missing at (100, 80)"
        assert _magenta_at(img, 300, 200), "input box stroke missing at (300, 200)"
        assert _has_chip(img, 100, 80), "label chip missing near (100, 80)"
