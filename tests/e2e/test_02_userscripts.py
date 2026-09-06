"""Userscripts (port of scrut 02-userscripts.md).

Userscripts are JS helpers that run at document_start on every
navigation, scoped per browser. The default userscripts
(logpoint-helpers.js) ship in every browser's scope. Assertion
source: verify_06.py's '02: userscripts' section.
"""

from __future__ import annotations

from tests.e2e.conftest import fixture_site

HELPER_SOURCE = (
    "if (!window.__usHelperRan__) window.__usHelperRan__ = 0;\n"
    "window.__usHelperRan__ += 1;\n"
)


async def test_userscript_install_source_and_file_modes(odda_session, tmp_path) -> None:
    """install accepts inline source or a JS file path; the file-mode script
    runs at document_start and both appear in userscript_list."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")

        r = await h.call(
            "userscript_install",
            {"browser_id": bid, "name": "helper", "source": HELPER_SOURCE},
        )
        assert r["name"] == "helper"
        assert r["size"] > 0

        us_file = tmp_path / "us_file.js"
        us_file.write_text("window.__usFromFile__ = 'file-ran';")
        r = await h.call(
            "userscript_install",
            {"browser_id": bid, "name": "fromfile", "file": str(us_file)},
        )
        assert r["name"] == "fromfile"
        assert r["size"] > 0

        await h.navigate(bid, tid, f"{fx.base}/")
        v = await h.wait_for(bid, tid, "window.__usFromFile__", timeout=5)
        assert v == "file-ran"

        r = await h.call("userscript_list", {"browser_id": bid})
        names = {s["name"] for s in r}
        # Containment only: wraps live as __odda-wrap__* userscripts too.
        assert {"helper", "fromfile"} <= names


async def test_userscript_install_validation_errors(odda_session, tmp_path) -> None:
    """install rejects file+source, neither, a missing file, empty source and a
    missing browser — param-name messages, verbatim."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, _tid = await h.open_browser(f"{fx.base}/")
        us_file = tmp_path / "us_file.js"
        us_file.write_text("window.__x__ = 1;")

        err = await h.call_error(
            "userscript_install",
            {"browser_id": bid, "name": "both", "file": str(us_file), "source": "1"},
        )
        assert err == "Provide either file or source, not both"

        err = await h.call_error(
            "userscript_install", {"browser_id": bid, "name": "neither"}
        )
        assert err == "Provide file <path> or source <js>"

        err = await h.call_error(
            "userscript_install",
            {
                "browser_id": bid,
                "name": "missing",
                "file": str(tmp_path / "no-such.js"),
            },
        )
        assert err.startswith("File not found:")

        err = await h.call_error(
            "userscript_install", {"browser_id": bid, "name": "empty", "source": "   "}
        )
        assert err == "source is empty"

        err = await h.call_error(
            "userscript_install", {"browser_id": 9999, "name": "x", "source": "1"}
        )
        assert err == "Browser 9999 not found."

        # list is a read: a missing browser yields an empty list, not an error.
        r = await h.call("userscript_list", {"browser_id": 9999})
        assert r == []


async def test_userscript_runs_on_every_navigation(odda_session) -> None:
    """The script runs at document_start on every navigation; a fresh page
    resets the counter, so an idempotent incrementing script reads 1 each time."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")
        await h.call(
            "userscript_install",
            {"browser_id": bid, "name": "helper", "source": HELPER_SOURCE},
        )

        await h.navigate(bid, tid, f"{fx.base}/")
        v = await h.eval(bid, tid, "String(window.__usHelperRan__)")
        assert v == "1"

        await h.navigate(bid, tid, f"{fx.base}/")
        v = await h.eval(bid, tid, "String(window.__usHelperRan__)")
        assert v == "1"


async def test_userscript_remove_stops_injection(odda_session, tmp_path) -> None:
    """remove reports name/removed/extension_id; after remove+navigate the
    marker is undefined; removing a missing name errors verbatim."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")
        us_file = tmp_path / "us_file.js"
        us_file.write_text("window.__usFromFile__ = 'file-ran';")
        await h.call(
            "userscript_install",
            {"browser_id": bid, "name": "fromfile", "file": str(us_file)},
        )

        r = await h.call("userscript_remove", {"browser_id": bid, "name": "fromfile"})
        assert r["name"] == "fromfile"
        assert r["removed"] is True
        assert isinstance(r["extension_id"], str)

        await h.navigate(bid, tid, f"{fx.base}/")
        v = await h.eval(bid, tid, "typeof window.__usFromFile__")
        assert v == "undefined"

        err = await h.call_error(
            "userscript_remove", {"browser_id": bid, "name": "no-such"}
        )
        assert err == "Userscript 'no-such' not found"
