"""Port of scrut 01-browser.md: browser, tabs, navigation, eval, screenshot,
event-listeners, wait-for.

Dropped as CLI-only dead surface (per the port briefing): ``eval --file``
(inline/file conflict + missing-file rejections) and the ``browser open
--help``/``--headless`` help-text pins. Everything else asserts on the
structured tool results.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.e2e.conftest import fixture_site


async def test_browser_list_rows_and_status_tracking(odda_session) -> None:
    """browser_list returns one {browser_id, tab_count} row per browser; a
    second browser gets id 2, and closing it restores single-browser state."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")
        assert (bid, tid) == (1, 1)

        r = await h.call("browser_list", {})
        assert r == [{"browser_id": 1, "tab_count": 1}]

        bid2, tid2 = await h.open_browser()
        assert (bid2, tid2) == (2, 1)
        r = await h.call("browser_list", {})
        assert r == [
            {"browser_id": 1, "tab_count": 1},
            {"browser_id": 2, "tab_count": 1},
        ]

        await h.call("browser_close", {"browser_id": 2})
        assert await h.call("browser_list", {}) == [{"browser_id": 1, "tab_count": 1}]

        # status.browser_count and browser_list's row count both derive
        # from BrowserManager._instances, so they cannot drift.
        r = await h.call("status", {})
        assert r["browser_count"] == 1
        assert len(await h.call("browser_list", {})) == r["browser_count"]

        await h.call("browser_close", {"browser_id": 1})
        assert await h.call("browser_list", {}) == []
        r = await h.call("status", {})
        assert r["browser_count"] == 0


async def test_eval_runs_js_and_renders_values(odda_session) -> None:
    """eval returns the raw value: strings without quotes, JSON.stringify
    output raw; plain objects render as JSON (the MCP text path
    pretty-prints them — the scrut doc pinned the CLI's compact
    single-line rendering, which is dead with the CLI)."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        assert await h.eval(bid, tid, "document.title") == "Listener Test"

        # JSON.stringify inside the JS returns a JS string — the text
        # arrives raw, no double-encoding.
        assert await h.eval(bid, tid, "JSON.stringify({a: 1})") == '{"a":1}'

        # A plain object return renders as JSON text.
        text = await h.eval(bid, tid, "({a: 1})")
        assert json.loads(text) == {"a": 1}
        assert '"a": 1' in text


async def test_screenshot_default_and_output_path(odda_session, tmp_path) -> None:
    """screenshot writes a JPEG and returns the path; output= targets an
    agent-chosen path whose parent directory is created."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        shot = await h.call("screenshot", {"browser_id": bid, "tab_id": tid})
        assert str(shot).endswith(".jpeg")
        assert Path(shot).is_file()

        target = tmp_path / "nested" / "dir" / "shot.jpeg"
        shot = await h.call(
            "screenshot", {"browser_id": bid, "tab_id": tid, "output": str(target)}
        )
        assert str(shot) == str(target)
        assert target.is_file()


async def test_event_listeners_lists_window_and_document_types(odda_session) -> None:
    """The fixture page registers a resize listener on window and a scroll
    listener on document; event_listeners contains at least those types."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        r = await h.call("event_listeners", {"browser_id": bid, "tab_id": tid})
        types = sorted({l["type"] for l in r})
        # The built-in dialog-interceptor userscript adds a `message`
        # listener, so containment (not the exact set) is the contract.
        assert {"resize", "scroll"} <= set(types)
        assert types == sorted(types)


async def test_wait_for_already_true(odda_session) -> None:
    """wait_for returns the truthy value immediately when the condition is
    already true."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")
        r = await h.wait_for(bid, tid, "document.title", timeout=5)
        assert r == "Listener Test"


async def test_wait_for_polls_until_delayed_value_lands(odda_session) -> None:
    """wait_for polls until a setTimeout-delayed value becomes truthy."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        await h.eval(
            bid, tid, "setTimeout(() => { window.__waitTest__ = 'arrived'; }, 1000)"
        )
        r = await h.wait_for(bid, tid, "window.__waitTest__", timeout=5)
        assert r == "arrived"


async def test_wait_for_times_out_when_never_truthy(odda_session) -> None:
    """wait_for errors with a descriptive Timeout message when the condition
    never becomes truthy — the message crosses the wire as a ToolError
    (BrowserOperationError), pinning the CLI-era contract the MCP port
    must preserve."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")
        err = await h.call_error(
            "wait_for",
            {
                "browser_id": bid,
                "tab_id": tid,
                "expression": "window.__never__",
                "timeout": 2,
            },
        )
        assert "Timeout" in err
        assert "2000.0ms" in err


async def test_wait_for_treats_thrown_error_as_falsy(odda_session) -> None:
    """A thrown error inside the expression is treated as falsy and polling
    continues: the fixture page has no #root, so the null deref keeps
    polling until the timeout instead of crashing on the first
    evaluation. Times out cleanly with the descriptive Timeout message
    (the thrown error never surfaces as a crash)."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")
        err = await h.call_error(
            "wait_for",
            {
                "browser_id": bid,
                "tab_id": tid,
                "expression": "document.querySelector('#root').children.length > 0",
                "timeout": 2,
            },
        )
        assert "Timeout" in err
