"""Session-level contracts: the in-process seam plus per-session state rules.

This module exercises the behavior layer over the in-process client: one
session, browser_open → navigate → eval → screenshot → browser_close, plus
the data-dir ownership contract. Corresponds to scrut 00-server.md's
surviving surface (version/proxy_url) plus 01-browser's core flow
(the old status probe died with the tool's removal).
"""

from __future__ import annotations

import re

from tests.e2e.conftest import fixture_site


async def test_session_boots_and_drives_real_chrome(odda_session) -> None:
    """browser_open → navigate → eval → screenshot → browser_close, all real."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        r = await h.call("version", {})
        # Shape, not value: the exact version lives in pyproject/__init__
        # and increments per release — this pin would go stale every bump.
        assert re.fullmatch(r"\d+\.\d+\.\d+", str(r["version"]))

        proxy = await h.call("proxy_url", {})
        assert str(proxy).startswith("http://127.0.0.1:")

        assert await h.call_json("browser_list", {}) == []

        bid, tid = await h.open_browser(f"{fx.base}/")
        assert (bid, tid) == (1, 1)

        r = await h.call_json("browser_list", {})
        assert r == [{"browser_id": 1, "tab_count": 1}]

        title = await h.eval(bid, tid, "document.title")
        assert title == "Listener Test"

        # screenshot writes a JPEG and returns the path (text-first →
        # the raw path arrives as the single text block).
        shot = await h.call("screenshot", {"browser_id": bid, "tab_id": tid})
        assert str(shot).endswith(".jpeg")

        await h.call("browser_close", {"browser_id": bid})
        assert await h.call_json("browser_list", {}) == []


async def test_data_dir_ownership_and_isolation(odda_session, tmp_path) -> None:
    """The session's lifespan owns .odda under the per-test cwd; flows stay
    absent until proxy traffic flows (Chrome background requests only land
    when a browser exists)."""
    async with odda_session() as h:
        # Pinned by the session factory itself (flowstore.DATA_DIR == data_dir);
        # here assert the observable surface: no read-only call creates the
        # data dir, and flows.jsonl stays absent until traffic flows (ADR-0017).
        assert not h.data_dir.exists()
        assert h.flow_count() == 0
