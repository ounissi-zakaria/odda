from __future__ import annotations

import base64
import json
import platform
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from tests.e2e.conftest import fixture_site, sans_display_urls


async def test_browser_list_rows_and_closing(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        assert await h.call("browser_list", {}) == []

        bid, tid = await h.open_browser(f"{fx.base}/")
        assert tid == 1

        r = await h.call("browser_list", {})
        assert sans_display_urls(r) == [
            {"browser_id": bid, "state": "open", "tab_count": 1}
        ]

        bid2, tid2 = await h.open_browser()
        assert tid2 == 1
        assert bid2 != bid
        r = await h.call("browser_list", {})
        assert sans_display_urls(r) == _by_id(
            [
                {"browser_id": bid, "state": "open", "tab_count": 1},
                {"browser_id": bid2, "state": "open", "tab_count": 1},
            ]
        )

        await h.call("browser_close", {"browser_id": bid2})
        assert sans_display_urls(await h.call("browser_list", {})) == _by_id(
            [
                {"browser_id": bid, "state": "open", "tab_count": 1},
                {"browser_id": bid2, "state": "closed"},
            ]
        )

        await h.call("browser_close", {"browser_id": bid})
        assert sans_display_urls(await h.call("browser_list", {})) == _by_id(
            [
                {"browser_id": bid, "state": "closed"},
                {"browser_id": bid2, "state": "closed"},
            ]
        )


async def test_open_runs_on_a_virtual_display(odda_session) -> None:
    # See ADR-0040.
    async with odda_session() as h:
        r = await h.call("browser_open", {})
        bid, tid = r["browser_id"], r["tab_id"]
        assert r["status"] == "launched"
        assert re.fullmatch(r":\d+", r["display"])
        m = re.fullmatch(r"vnc://127\.0\.0\.1:(\d+)", r["vnc_url"])
        assert m, r["vnc_url"]
        rfb_port = int(m.group(1))
        with socket.create_connection(("127.0.0.1", rfb_port), timeout=5) as s:
            assert s.recv(12).startswith(b"RFB ")

        m = re.fullmatch(r"http://127\.0\.0\.1:(\d+)/vnc\.html", r["novnc_url"])
        assert m, r["novnc_url"]
        novnc_port = int(m.group(1))
        assert novnc_port != rfb_port
        with urllib.request.urlopen(  # noqa: S310 - http scheme asserted above
            r["novnc_url"], timeout=5
        ) as resp:
            assert b"noVNC" in resp.read(4096)

        assert (
            await h.eval(bid, tid, "[screen.width, screen.height].join('x')")
            == "1440x900"
        )

        await h.call("browser_close", {"browser_id": bid})
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", rfb_port), timeout=1)
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", novnc_port), timeout=1)


@pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGTERM], ids=str)
def test_hard_killed_session_cannot_leak_its_display(sig) -> None:
    # See ADR-0040.
    child = (
        "import asyncio, json, os, signal\n"
        "from odda.vdisplay import start_virtual_display\n"
        "d = asyncio.run(start_virtual_display(640, 480, '127.0.0.1'))\n"
        "print(json.dumps({'display': d.display, 'rfb': d.rfb_port,"
        " 'http': d.http_port}), flush=True)\n"
        f"os.kill(os.getpid(), {int(sig)})\n"
    )
    proc = subprocess.run(  # noqa: S603 - pinned interpreter, local source
        [sys.executable, "-c", child],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert proc.returncode == -int(sig), proc.stderr[-500:]
    info = json.loads(proc.stdout.strip().splitlines()[-1])
    display_num = info["display"].lstrip(":")
    rfb, http = int(info["rfb"]), int(info["http"])
    lock = Path(f"/tmp/.X{display_num}-lock")  # noqa: S108 - X11 lock

    def display_down() -> bool:
        return not _port_open(rfb) and not _port_open(http) and not lock.exists()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not display_down():
        time.sleep(0.25)
    assert not _port_open(rfb), "Xvnc survived its killed parent"
    assert not _port_open(http), "websockify survived its killed parent"
    assert not lock.exists(), "SIGTERMed Xvnc left its lock behind"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


async def test_eval_runs_js_and_renders_values(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        assert await h.eval(bid, tid, "document.title") == "Listener Test"

        assert await h.eval(bid, tid, "JSON.stringify({a: 1})") == '{"a":1}'

        text = await h.eval(bid, tid, "({a: 1})")
        assert json.loads(text) == {"a": 1}
        assert '"a": 1' in text


async def test_eval_file_mode_and_validation(odda_session, tmp_path) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        script = tmp_path / "probe.js"
        script.write_text("(function () {\n  return 6 * 7;\n})()")
        r = await h.call(
            "eval", {"browser_id": bid, "tab_id": tid, "file": str(script)}
        )
        assert str(r).strip() == "42"

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
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        r = await h.call("event_listeners", {"browser_id": bid, "tab_id": tid})
        types = sorted({l["type"] for l in r})
        assert {"resize", "scroll"} <= set(types)
        assert types == sorted(types)


async def test_wait_for_already_true(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")
        r = await h.wait_for(bid, tid, "document.title", timeout=5)
        assert r == "Listener Test"


async def test_wait_for_polls_until_delayed_value_lands(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        await h.eval(
            bid, tid, "setTimeout(() => { window.__waitTest__ = 'arrived'; }, 1000)"
        )
        r = await h.wait_for(bid, tid, "window.__waitTest__", timeout=5)
        assert r == "arrived"


async def test_wait_for_times_out_when_never_truthy(odda_session) -> None:
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


# See ADR-0032.


def _by_id(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda row: row["browser_id"])


async def test_reopen_record_restores_profile(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")
        await h.eval(bid, tid, "localStorage.setItem('persisted', 'yes') || 'set'")
        await h.call("tabs_open", {"browser_id": bid, "url": f"{fx.base}/"})
        rows = await h.call("tabs_list", {"browser_id": bid})
        assert len(rows[0]["tabs"]) == 2

        assert (h.data_dir / "browsers" / bid / "profile").is_dir()

        await h.call("browser_close", {"browser_id": bid})
        assert await h.call("browser_list", {}) == [
            {"browser_id": bid, "state": "closed"}
        ]

        r = await h.call("browser_open", {"browser_id": bid})
        assert r["browser_id"] == bid
        assert r["tab_id"] == 1
        assert sans_display_urls(await h.call("browser_list", {})) == [
            {"browser_id": bid, "state": "open", "tab_count": 1}
        ]

        await h.navigate(bid, r["tab_id"], f"{fx.base}/")
        assert (
            await h.eval(bid, r["tab_id"], "localStorage.getItem('persisted')") == "yes"
        )


async def test_open_errors_unknown_and_open_browsers(odda_session) -> None:
    async with odda_session() as h:
        err = await h.call_error("browser_open", {"browser_id": "zzzzz"})
        assert err == "Browser zzzzz not found."

        bid, _tid = await h.open_browser()
        err = await h.call_error("browser_open", {"browser_id": bid})
        assert err == f"Browser {bid} is already open."
        err = await h.call_error("browser_open", {"browser_id": bid.upper()})
        assert err == f"Browser {bid} is already open."


async def test_stale_lock_reads_closed(odda_session) -> None:
    async with odda_session() as h:
        dead = next(
            p for p in range(4194000, 4193000, -1) if not Path(f"/proc/{p}").exists()
        )
        record = h.data_dir / "browsers" / "qwert"
        (record / "profile").mkdir(parents=True)
        (record / "userscripts-extension").mkdir()
        (record / "profile" / "SingletonLock").symlink_to(f"{platform.node()}-{dead}")

        assert await h.call("browser_list", {}) == [
            {"browser_id": "qwert", "state": "closed"}
        ]
        r = await h.call("browser_open", {"browser_id": "qwert"})
        assert r["browser_id"] == "qwert"


async def test_legacy_dirs_are_not_records(odda_session) -> None:
    async with odda_session() as h:
        browsers = h.data_dir / "browsers"
        (browsers / "1").mkdir(parents=True)
        (browsers / "abcde" / "userscripts-extension").mkdir(parents=True)

        assert await h.call("browser_list", {}) == []
        err = await h.call_error("browser_open", {"browser_id": "abcde"})
        assert err == "Browser abcde not found."


async def test_base_profile_seeds_creation_only(odda_session, config_home) -> None:
    base = config_home / "chrome-profile"
    base.mkdir()
    (base / "seed-marker").write_text("seeded")

    async with odda_session() as h:
        bid, _tid = await h.open_browser()
        profile = h.data_dir / "browsers" / bid / "profile"
        assert (profile / "seed-marker").read_text() == "seeded"

        (profile / "seed-marker").unlink()
        await h.call("browser_close", {"browser_id": bid})
        r = await h.call("browser_open", {"browser_id": bid})
        assert r["browser_id"] == bid
        assert not (profile / "seed-marker").exists()


async def test_open_in_another_session(odda_session) -> None:
    async with odda_session() as h1, odda_session() as h2:
        bid, _tid = await h1.open_browser()

        assert await h2.call("browser_list", {}) == [
            {"browser_id": bid, "state": "open in another session"}
        ]
        err = await h2.call_error("browser_open", {"browser_id": bid})
        assert err == f"Browser {bid} is open in another session."

        await h1.call("browser_close", {"browser_id": bid})
        assert await h2.call("browser_list", {}) == [
            {"browser_id": bid, "state": "closed"}
        ]
        r = await h2.call("browser_open", {"browser_id": bid})
        assert r["browser_id"] == bid
        assert sans_display_urls(await h2.call("browser_list", {})) == [
            {"browser_id": bid, "state": "open", "tab_count": 1}
        ]
