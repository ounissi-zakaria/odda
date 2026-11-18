from __future__ import annotations

import base64
import csv
import io
import json
import re
from io import BytesIO
from pathlib import Path

from PIL import Image

from tests.e2e.conftest import fixture_site


def _pluck_ref(snapshot: str, text: str, pattern: str = r"\[ref=(e\d+)\]") -> str:
    line = next(l for l in snapshot.splitlines() if text in l)
    m = re.search(pattern, line)
    assert m, f"no {pattern!r} on line: {line!r}"
    return m.group(1)


async def test_page_snapshot_and_ref_clicks(odda_session) -> None:
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
        assert isinstance(snap, str) and "[ref=e" in snap
        assert "Click me" in snap
        assert "textbox" in snap.lower() or "text" in snap.lower()
        assert re.search(r"\[ref=f\d+e\d+\]", snap)
        assert "Iframe button" in snap

        ref = _pluck_ref(snap, "Click me")
        r = await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})
        assert r["status"] == "clicked" and r["ref"] == ref
        out = await h.eval(
            bid, tid, "document.getElementById('click-result').textContent"
        )
        assert out == "clicked"

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

        r = await h.call(
            "page_click", {"browser_id": bid, "tab_id": tid, "x": 100, "y": 330}
        )
        assert r["status"] == "clicked"
        assert (r["x"], r["y"]) == (100, 330)
        out = await h.eval(
            bid, tid, "document.getElementById('coord-click-result').textContent"
        )
        assert out == "coord clicked @ 100,330"

        r = await h.call(
            "page_hover", {"browser_id": bid, "tab_id": tid, "x": 250, "y": 330}
        )
        assert r["status"] == "hovered"
        assert (r["x"], r["y"]) == (250, 330)
        out = await h.eval(
            bid, tid, "document.getElementById('coord-hover-result').textContent"
        )
        assert out == "coord hovered @ 250,330"

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
        assert re.fullmatch(r"coord in iframe @ 5\d*,11\d*", out), out

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

        await h.call(
            "page_fill",
            {"browser_id": bid, "tab_id": tid, "ref": tref, "value": "world"},
        )
        out = await h.eval(bid, tid, "document.getElementById('text-input').value")
        assert out == "world"

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

        href = _pluck_ref(snap, "Hover me")
        r = await h.call("page_hover", {"browser_id": bid, "tab_id": tid, "ref": href})
        assert r["status"] == "hovered" and r["ref"] == href
        out = await h.eval(
            bid, tid, "document.getElementById('hover-result').textContent"
        )
        assert out == "hovered"

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

        await h.navigate(bid, tid, "about:blank")
        err = await h.call_error(
            "page_click",
            {"browser_id": bid, "tab_id": tid, "ref": href, "timeout": 2},
        )
        assert re.search(r"did not resolve or become actionable within", err)

        err = await h.call_error(
            "page_click", {"browser_id": bid, "tab_id": 9999, "ref": "e1"}
        )
        assert err == f"Tab 9999 not found in browser {bid}."
        err = await h.call_error(
            "page_fill",
            {"browser_id": "zzzzz", "tab_id": tid, "ref": "e1", "value": "x"},
        )
        assert err == "Browser zzzzz not found."
        err = await h.call_error("page_snapshot", {"browser_id": bid, "tab_id": 9999})
        assert err == f"Tab 9999 not found in browser {bid}."
        err = await h.call_error(
            "page_snapshot", {"browser_id": "zzzzz", "tab_id": tid}
        )
        assert err == "Browser zzzzz not found."


_MAGENTA_RADIUS = 4


def _is_magentaish(r: int, g: int, b: int) -> bool:
    return r > 200 and b > 200 and g < 120


def _magenta_at(img: Image.Image, x: int, y: int) -> bool:
    px = img.load()
    for xx in range(x - _MAGENTA_RADIUS, x + _MAGENTA_RADIUS + 1):
        for yy in range(y - _MAGENTA_RADIUS, y + _MAGENTA_RADIUS + 1):
            if 0 <= xx < img.width and 0 <= yy < img.height:
                r, g, b = px[xx, yy]
                if _is_magentaish(r, g, b):
                    return True
    return False


def _has_chip(img: Image.Image, x: int, y: int) -> bool:
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
    body = (
        '<button id="go" style="position:absolute;left:100px;top:80px;'
        'width:120px;height:40px;">Go</button>'
        '<input id="q" aria-label="Query" style="position:absolute;'
        'left:300px;top:200px;width:200px;height:32px;">'
    )
    async with odda_session() as h, fixture_site(index_body=body) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        plain_r = await h.client.call_tool(
            "screenshot", {"browser_id": bid, "tab_id": tid}
        )
        assert not plain_r.is_error
        assert [b.type for b in plain_r.content] == ["text", "image"]
        plain = plain_r.content[0].text
        assert str(plain).endswith(".jpeg")
        plain_img = Image.open(BytesIO(Path(plain).read_bytes())).convert("RGB")
        assert not _magenta_at(plain_img, 100, 80)

        r = await h.client.call_tool(
            "screenshot",
            {"browser_id": bid, "tab_id": tid, "annotate": True},
        )
        assert not r.is_error
        assert [b.type for b in r.content] == ["text", "text", "image"]
        shot = r.content[0].text
        legend = r.content[1].text
        assert str(shot).endswith(".jpeg")
        assert Path(shot).read_bytes() == base64.b64decode(r.content[2].data)

        snap = await h.call(
            "page_snapshot", {"browser_id": bid, "tab_id": tid, "boxes": True}
        )
        snap_boxes = {}
        for line in snap.splitlines():
            m = re.search(
                r"\[ref=((?:f\d+)?e\d+)\] \[box=(-?\d+),(-?\d+),(-?\d+),(-?\d+)\]",
                line,
            )
            if m:
                snap_boxes[m.group(1)] = tuple(int(m.group(i)) for i in range(2, 6))
        button_ref = _pluck_ref(snap, "Go")
        input_ref = _pluck_ref(snap, "Query")

        rows = list(csv.DictReader(io.StringIO(legend)))
        by_ref = {row["ref"]: row for row in rows}
        assert {button_ref, input_ref} <= by_ref.keys()
        assert sorted(int(row["n"]) for row in rows) == list(range(1, len(rows) + 1))
        for ref in (button_ref, input_ref):
            row = by_ref[ref]
            assert tuple(int(row[k]) for k in ("x", "y", "w", "h")) == snap_boxes[ref]

        img = Image.open(BytesIO(Path(shot).read_bytes())).convert("RGB")
        assert _magenta_at(img, 100, 80), "button box stroke missing at (100, 80)"
        assert _magenta_at(img, 300, 200), "input box stroke missing at (300, 200)"
        assert _has_chip(img, 100, 80), "marker chip missing near (100, 80)"
