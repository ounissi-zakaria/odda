"""Thin stdio leg: pin the SDK-owned stdio wire (initialize → tools/list → call → exit).

The primary harness is in-process (test modules); this module alone
exercises a real ``python -m odda.mcp`` subprocess over stdio, exactly
as a harness (Claude/OpenCode/pi/omp) spawns it. Keeps the transport
honest without duplicating behavior coverage.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters


@pytest.mark.asyncio
async def test_stdio_subprocess_round_trip(tmp_path: Path) -> None:
    """Real subprocess: initialize → tools/list → browser_open/page_snapshot/browser_close."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "odda.mcp"],
        # cwd pins the data dir: resolve_data_dir() is cwd-`.odda`-relative
        # (ODDA_DATA_DIR died with the CLI — ticket #07). tmp_path isolates
        # the subprocess's flows/browsers from the repo's own .odda/.
        cwd=str(tmp_path),
        env={**os.environ},
    )
    async with Client(params) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        # 42 tools registered on this branch (#04–#07's ported surface).
        assert len(names) == 42, f"expected 42 tools, got {len(names)}"
        for expected in (
            "browser_open",
            "navigate",
            "eval",
            "wait_for",
            "screenshot",
            "page_snapshot",
            "page_click",
            "page_fill",
            "page_hover",
            "page_upload",
            "request_new",
            "request_clone",
            "request_send",
            "proxy_url",
            "coverage_start",
            "wrap_calls_add",
            "logpoint_add",
            "userscript_install",
            "proxy_script_install",
            "status",
            "version",
        ):
            assert expected in names, f"missing tool {expected}"

        # one full round trip over the wire
        r = await client.call_tool("browser_open", {"headless": True})
        assert not r.is_error
        sc = r.structured_content
        bid, tid = sc["browser_id"], sc["tab_id"]

        r = await client.call_tool("page_snapshot", {"browser_id": bid, "tab_id": tid})
        assert not r.is_error

        r = await client.call_tool("browser_close", {"browser_id": bid})
        assert not r.is_error

    # Client exit closed stdin → the subprocess should be gone shortly.
    # (The SDK's stdio transport waits for process exit on __aexit__; a
    # surviving process here would mean odda.mcp ignores stdin close.)
