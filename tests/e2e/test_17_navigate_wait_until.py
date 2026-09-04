"""navigate: timeout and wait_until for SPAs that defer `load` (scrut 17).

The SPA page's inline JS runs at DOMContentLoaded but a 35s slow image
blocks window.onload. navigate's timeout bounds the wait and its error
names the wait_until event that timed out; wait_until picks an earlier
lifecycle event (commit, domcontentloaded) that resolves immediately.
The tool's input schema pins the defaults: timeout 30, wait_until load.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from tests.e2e.conftest import FIXTURES, slow_load_server


def _site_dir(tmp_path) -> Path:
    """Build the slow-load site dir (spa-slow-load.html copied from fixtures)."""
    site = tmp_path / "spa-site"
    site.mkdir()
    (site / "spa-slow-load.html").write_bytes(
        (FIXTURES / "spa-slow-load.html").read_bytes()
    )
    return site


async def test_navigate_timeout_errors_and_names_load_event(
    odda_session, tmp_path
) -> None:
    """timeout=3 errors with `Timeout` + `wait-until \`load\`` in well under 12s."""
    site = _site_dir(tmp_path)
    async with odda_session() as h, slow_load_server(site) as srv:
        bid, tid = await h.open_browser()

        url = f"{srv.base}/spa-slow-load.html"
        start = time.monotonic()
        err = await h.call_error(
            "navigate",
            {"browser_id": bid, "tab_id": tid, "url": url, "timeout": 3},
        )
        elapsed = time.monotonic() - start

        assert "Timeout" in err
        assert "wait-until `load`" in err
        assert elapsed < 12, f"navigate with timeout=3 took {elapsed:.1f}s"

        await h.call("browser_close", {"browser_id": bid})


async def test_navigate_wait_until_domcontentloaded_succeeds(
    odda_session, tmp_path
) -> None:
    """wait_until=domcontentloaded resolves immediately; the SPA already ran."""
    site = _site_dir(tmp_path)
    async with odda_session() as h, slow_load_server(site) as srv:
        bid, tid = await h.open_browser()

        url = f"{srv.base}/spa-slow-load.html"
        nav = await h.navigate(bid, tid, url, wait_until="domcontentloaded")
        assert nav["status"].startswith("Navigated to: ")

        # eval is Any-annotated → text; the object renders as JSON.
        text = await h.eval(
            bid,
            tid,
            "({ready: window.__spaReady,"
            " text: document.getElementById('app').textContent})",
        )
        try:
            obj = json.loads(text)
            assert obj["ready"] is True
            assert "SPA ready at" in obj["text"]
        except (json.JSONDecodeError, TypeError):
            assert "SPA ready at" in text

        await h.call("browser_close", {"browser_id": bid})


async def test_navigate_wait_until_commit_succeeds(odda_session, tmp_path) -> None:
    """wait_until=commit resolves once the navigation response is received."""
    site = _site_dir(tmp_path)
    async with odda_session() as h, slow_load_server(site) as srv:
        bid, tid = await h.open_browser()

        url = f"{srv.base}/spa-slow-load.html"
        nav = await h.navigate(bid, tid, url, wait_until="commit")
        assert nav["status"].startswith("Navigated to: ")

        await h.call("browser_close", {"browser_id": bid})


async def test_navigate_wait_until_networkidle_times_out(
    odda_session, tmp_path
) -> None:
    """wait_until=networkidle timeout=3 errors naming `networkidle` (slow image
    keeps a connection open 35s)."""
    site = _site_dir(tmp_path)
    async with odda_session() as h, slow_load_server(site) as srv:
        bid, tid = await h.open_browser()

        url = f"{srv.base}/spa-slow-load.html"
        err = await h.call_error(
            "navigate",
            {
                "browser_id": bid,
                "tab_id": tid,
                "url": url,
                "wait_until": "networkidle",
                "timeout": 3,
            },
        )
        assert "Timeout" in err
        assert "wait-until `networkidle`" in err

        await h.call("browser_close", {"browser_id": bid})


async def test_navigate_wait_until_rejects_invalid_event(
    odda_session, tmp_path
) -> None:
    """wait_until=bogus errors naming the wait_until param."""
    async with odda_session() as h:
        bid, tid = await h.open_browser()

        err = await h.call_error(
            "navigate",
            {
                "browser_id": bid,
                "tab_id": tid,
                "url": "about:blank",
                "wait_until": "bogus",
            },
        )
        assert "wait_until" in err

        await h.call("browser_close", {"browser_id": bid})


async def test_navigate_input_schema_defaults(odda_session, tmp_path) -> None:
    """navigate's tool schema defaults: timeout 30.0, wait_until 'load'."""
    async with odda_session() as h:
        tools = await h.client.list_tools()
        tool = next(t for t in tools.tools if t.name == "navigate")
        props = tool.input_schema["properties"]
        assert props["timeout"]["default"] == 30.0
        assert props["wait_until"]["default"] == "load"
