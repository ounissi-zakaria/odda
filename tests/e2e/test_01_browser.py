"""Port of scrut 01-browser.md: browser, tabs, navigation, eval, screenshot,
event-listeners, wait-for.

``eval``'s file mode is back (brought back on the MCP surface: js xor
file, param-name messages). Dropped as CLI-only dead surface (per the
port briefing): the ``browser open --help``/``--headless`` help-text
pins. Everything else asserts on the structured tool results.
"""

from __future__ import annotations

import base64
import json
import platform
import re
from pathlib import Path

from tests.e2e.conftest import fixture_site


async def test_browser_list_rows_and_closing(odda_session) -> None:
    """browser_list lists every browser record alphabetically with its state:
    this session's open browsers carry tab_count; a closed browser persists
    as a closed row (no tab_count — its Chrome is gone, not its record)."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        assert await h.call_json("browser_list", {}) == []

        bid, tid = await h.open_browser(f"{fx.base}/")
        assert tid == 1

        r = await h.call_json("browser_list", {})
        assert r == [{"browser_id": bid, "state": "open", "tab_count": 1}]

        bid2, tid2 = await h.open_browser()
        assert tid2 == 1
        assert bid2 != bid
        r = await h.call_json("browser_list", {})
        assert r == _by_id(
            [
                {"browser_id": bid, "state": "open", "tab_count": 1},
                {"browser_id": bid2, "state": "open", "tab_count": 1},
            ]
        )

        await h.call("browser_close", {"browser_id": bid2})
        assert await h.call_json("browser_list", {}) == _by_id(
            [
                {"browser_id": bid, "state": "open", "tab_count": 1},
                {"browser_id": bid2, "state": "closed"},
            ]
        )

        await h.call("browser_close", {"browser_id": bid})
        assert await h.call_json("browser_list", {}) == _by_id(
            [
                {"browser_id": bid, "state": "closed"},
                {"browser_id": bid2, "state": "closed"},
            ]
        )


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


async def test_eval_file_mode_and_validation(odda_session, tmp_path) -> None:
    """eval js xor file: file reads a multi-line script from a server-side
    path; both/neither combos and a missing file error with param-name
    messages."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        # file mode: multi-line IIFE, executed verbatim.
        script = tmp_path / "probe.js"
        script.write_text("(function () {\n  return 6 * 7;\n})()")
        r = await h.call(
            "eval", {"browser_id": bid, "tab_id": tid, "file": str(script)}
        )
        assert str(r).strip() == "42"

        # both / neither / missing file.
        err = await h.call_error(
            "eval",
            {"browser_id": bid, "tab_id": tid, "js": "1", "file": str(script)},
        )
        assert err == "Provide either js or file, not both"
        err = await h.call_error("eval", {"browser_id": bid, "tab_id": tid})
        assert err == "Provide js <code> or file <path>"
        err = await h.call_error(
            "eval",
            {"browser_id": bid, "tab_id": tid, "file": str(tmp_path / "no-such.js")},
        )
        assert err.startswith("File not found:")

        await h.call("browser_close", {"browser_id": bid})


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


async def test_screenshot_inline_pixels_default_and_opt_out(odda_session) -> None:
    """By default screenshot returns the path as the first text block
    followed by an inline JPEG image block whose bytes are the written
    file; return_image=False restores the bare path-only result."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        r = await h.client.call_tool("screenshot", {"browser_id": bid, "tab_id": tid})
        assert not r.is_error
        assert [b.type for b in r.content] == ["text", "image"]
        path = r.content[0].text
        assert path.endswith(".jpeg")
        assert Path(path).is_file()
        img = r.content[1]
        assert img.mime_type == "image/jpeg"
        # The inline block is the persisted file, verbatim — no re-encode.
        assert base64.b64decode(img.data) == Path(path).read_bytes()

        r = await h.client.call_tool(
            "screenshot",
            {"browser_id": bid, "tab_id": tid, "return_image": False},
        )
        assert not r.is_error
        assert [b.type for b in r.content] == ["text"]
        assert r.content[0].text.endswith(".jpeg")

        await h.call("browser_close", {"browser_id": bid})


async def test_event_listeners_lists_window_and_document_types(odda_session) -> None:
    """The fixture page registers a resize listener on window and a scroll
    listener on document; event_listeners contains at least those types."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        r = await h.call_json("event_listeners", {"browser_id": bid, "tab_id": tid})
        types = sorted({l["type"] for l in r})
        # Built-in default userscripts still ship in every browser and
        # may register listeners, so containment (not the exact set) is
        # the contract.
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


# --- durable browser records (ADR-0032) ---------------------------------


def _by_id(rows: list[dict]) -> list[dict]:
    """Rows in the browser_list order: sorted by browser_id."""
    return sorted(rows, key=lambda row: row["browser_id"])


async def test_reopen_record_restores_profile(odda_session) -> None:
    """browser_open(browser_id=...) reopens a closed record: same token, the
    profile survives the close (localStorage persists), and the browser comes
    back with exactly one fresh tab."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")
        await h.eval(bid, tid, "localStorage.setItem('persisted', 'yes') || 'set'")
        await h.call("tabs_open", {"browser_id": bid, "url": f"{fx.base}/"})
        rows = await h.call_json("tabs_list", {"browser_id": bid})
        assert len(rows[0]["tabs"]) == 2

        # The record is the directory: profile/ exists from first launch.
        assert (h.data_dir / "browsers" / bid / "profile").is_dir()

        await h.call("browser_close", {"browser_id": bid})
        assert await h.call_json("browser_list", {}) == [
            {"browser_id": bid, "state": "closed"}
        ]

        r = await h.call("browser_open", {"browser_id": bid, "headless": True})
        assert r["browser_id"] == bid
        assert r["tab_id"] == 1
        assert await h.call_json("browser_list", {}) == [
            {"browser_id": bid, "state": "open", "tab_count": 1}
        ]

        await h.navigate(bid, r["tab_id"], f"{fx.base}/")
        assert (
            await h.eval(bid, r["tab_id"], "localStorage.getItem('persisted')") == "yes"
        )


async def test_open_errors_unknown_and_open_browsers(odda_session) -> None:
    """browser_open with an explicit id errors on unknown records and on
    browsers already open here; ids are case-insensitive."""
    async with odda_session() as h:
        err = await h.call_error("browser_open", {"browser_id": "zzzzz"})
        assert err == "Browser zzzzz not found."

        bid, _tid = await h.open_browser()
        err = await h.call_error("browser_open", {"browser_id": bid})
        assert err == f"Browser {bid} is already open."
        err = await h.call_error("browser_open", {"browser_id": bid.upper()})
        assert err == f"Browser {bid} is already open."


async def test_stale_lock_reads_closed(odda_session) -> None:
    """A SingletonLock left behind by a dead Chrome (kill -9, crashed session)
    reads as closed: the record is listed closed and can be reopened —
    Chrome reclaims its own stale lock at launch."""
    async with odda_session() as h:
        # A pid with no /proc entry: the lock's owner is gone. Linux
        # pid_max defaults to 4194304, so scan down from just below it
        # for a pid no live process holds.
        dead = next(
            p for p in range(4194000, 4193000, -1) if not Path(f"/proc/{p}").exists()
        )
        record = h.data_dir / "browsers" / "qwert"
        (record / "profile").mkdir(parents=True)
        (record / "userscripts-extension").mkdir()
        # Same-host lock with a dead pid: odda's signal-0 probe sees the
        # owner is gone, and Chrome reclaims the lock silently at
        # launch. A foreign hostname would make Chrome pop its "profile
        # in use on another computer" dialog instead — never fake the
        # hostname here.
        (record / "profile" / "SingletonLock").symlink_to(f"{platform.node()}-{dead}")

        assert await h.call_json("browser_list", {}) == [
            {"browser_id": "qwert", "state": "closed"}
        ]
        r = await h.call("browser_open", {"browser_id": "qwert", "headless": True})
        assert r["browser_id"] == "qwert"


async def test_legacy_dirs_are_not_records(odda_session) -> None:
    """Directories without profile/ are not browser records: legacy numeric
    dirs and pre-record userscript-only dirs are never listed and never
    openable."""
    async with odda_session() as h:
        browsers = h.data_dir / "browsers"
        (browsers / "1").mkdir(parents=True)
        (browsers / "abcde" / "userscripts-extension").mkdir(parents=True)

        assert await h.call_json("browser_list", {}) == []
        err = await h.call_error("browser_open", {"browser_id": "abcde"})
        assert err == "Browser abcde not found."


async def test_base_profile_seeds_creation_only(
    odda_session, tmp_path, monkeypatch
) -> None:
    """A new record's profile is seeded from the base profile at creation;
    reopen never re-seeds — the record's own diverged state wins."""
    import odda.browser as odda_browser

    base = tmp_path / "base-profile"
    base.mkdir()
    (base / "seed-marker").write_text("seeded")
    monkeypatch.setattr(odda_browser, "BASE_PROFILE_DIR", base)

    async with odda_session() as h:
        bid, _tid = await h.open_browser()
        profile = h.data_dir / "browsers" / bid / "profile"
        assert (profile / "seed-marker").read_text() == "seeded"

        # Diverge from the seed, close, reopen: the seed must not come back.
        (profile / "seed-marker").unlink()
        await h.call("browser_close", {"browser_id": bid})
        r = await h.call("browser_open", {"browser_id": bid, "headless": True})
        assert r["browser_id"] == bid
        assert not (profile / "seed-marker").exists()


async def test_open_in_another_session(odda_session) -> None:
    """Two MCP sessions on one project's data dir: a browser open in one
    lists as 'open in another session' in the other and cannot be opened
    there; once closed, the other session can reopen the record."""
    async with odda_session() as h1, odda_session() as h2:
        bid, _tid = await h1.open_browser()

        assert await h2.call_json("browser_list", {}) == [
            {"browser_id": bid, "state": "open in another session"}
        ]
        err = await h2.call_error("browser_open", {"browser_id": bid})
        assert err == f"Browser {bid} is open in another session."

        await h1.call("browser_close", {"browser_id": bid})
        assert await h2.call_json("browser_list", {}) == [
            {"browser_id": bid, "state": "closed"}
        ]
        r = await h2.call("browser_open", {"browser_id": bid, "headless": True})
        assert r["browser_id"] == bid
        assert await h2.call_json("browser_list", {}) == [
            {"browser_id": bid, "state": "open", "tab_count": 1}
        ]
