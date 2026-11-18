# See ADR-0022.
from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import odda.mcp as odda_mcp
from tests.e2e.conftest import fixture_site, odda_session


def _site_pathname(fx, name: str) -> str:
    return f"{urlparse(fx.base).path}/{name}"


def _pluck_ref(snapshot: str, label: str) -> str:
    line = next(l for l in snapshot.splitlines() if label in l)
    return re.search(r"\[ref=(\w+)\]", line).group(1)


async def test_trigger_rule_three_tools(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html", "dialogs.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/dialogs.html")

        ref = _pluck_ref(await h.page_snapshot(bid, tid), "Confirm me")
        r = await asyncio.wait_for(
            h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref}), 10
        )
        assert r["dialog"] == {
            "type": "confirm",
            "message": "confirm-message",
            "default_value": "",
            "tab_id": tid,
        }
        await h.call(
            "dialog_handle", {"browser_id": bid, "tab_id": tid, "action": "dismiss"}
        )

        r = await asyncio.wait_for(
            h.eval(bid, tid, "(window.alert('alert-from-eval'), 42)"), 10
        )
        assert json.loads(r)["dialog"] == {
            "type": "alert",
            "message": "alert-from-eval",
            "default_value": "",
            "tab_id": tid,
        }
        await h.call(
            "dialog_handle", {"browser_id": bid, "tab_id": tid, "action": "accept"}
        )
        r = await asyncio.wait_for(
            h.wait_for(
                bid, tid, "(window.confirm('wait-for-message'), false)", timeout=3
            ),
            10,
        )
        assert json.loads(r)["dialog"]["message"] == "wait-for-message"
        await h.call(
            "dialog_handle", {"browser_id": bid, "tab_id": tid, "action": "dismiss"}
        )


async def test_blocking_rejects_same_tab_tools(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html", "dialogs.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/dialogs.html")
        ref = _pluck_ref(await h.page_snapshot(bid, tid), "Confirm me")
        trigger = asyncio.create_task(
            h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})
        )
        await asyncio.wait_for(trigger, 10)

        expected = (
            f"Tab {tid} has an open confirm dialog ('confirm-message') "
            "— handle it with dialog_handle, then retry this call."
        )
        err = await h.call_error(
            "eval", {"browser_id": bid, "tab_id": tid, "js": "1 + 1"}
        )
        assert err == expected
        err = await h.call_error("page_snapshot", {"browser_id": bid, "tab_id": tid})
        assert err == expected

        await h.call(
            "dialog_handle", {"browser_id": bid, "tab_id": tid, "action": "accept"}
        )
        assert await h.eval(bid, tid, "1 + 1") == "2"
        assert "Confirm me" in await h.page_snapshot(bid, tid)
        assert await h.wait_for(bid, tid, "window.__confirmResult === true", timeout=5)


async def test_dialog_handle_semantics(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html", "dialogs.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/dialogs.html")
        await h.eval(bid, tid, "window.name = ''")

        ref = _pluck_ref(await h.page_snapshot(bid, tid), "Confirm me")
        await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})
        r = await h.call(
            "dialog_handle", {"browser_id": bid, "tab_id": tid, "action": "accept"}
        )
        assert r == {"handled": True, "action": "accept", "tab_id": tid}
        assert await h.wait_for(bid, tid, "window.__confirmResult === true", timeout=5)

        await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})
        r = await h.call(
            "dialog_handle", {"browser_id": bid, "tab_id": tid, "action": "dismiss"}
        )
        assert r == {"handled": True, "action": "dismiss", "tab_id": tid}
        assert await h.wait_for(bid, tid, "window.__confirmResult === false", timeout=5)

        ref = _pluck_ref(await h.page_snapshot(bid, tid), "Prompt me")
        await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})
        r = await h.call(
            "dialog_handle",
            {
                "browser_id": bid,
                "tab_id": tid,
                "action": "accept",
                "prompt_text": "typed-answer",
            },
        )
        assert r == {"handled": True, "action": "accept", "tab_id": tid}
        assert await h.wait_for(
            bid, tid, "window.__promptResult === 'typed-answer'", timeout=5
        )

        await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})
        await h.call(
            "dialog_handle", {"browser_id": bid, "tab_id": tid, "action": "accept"}
        )
        assert await h.wait_for(
            bid, tid, "window.__promptResult === 'prefilled'", timeout=5
        )

        await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})
        await h.call(
            "dialog_handle", {"browser_id": bid, "tab_id": tid, "action": "dismiss"}
        )
        assert await h.wait_for(bid, tid, "window.__promptResult === null", timeout=5)

        err = await h.call_error(
            "dialog_handle", {"browser_id": bid, "tab_id": tid, "action": "accept"}
        )
        assert err == f"Tab {tid} in browser {bid} has no open dialog."


async def test_user_close_clears_and_unblocks(odda_session) -> None:
    lowlevel = odda_mcp.mcp_server._lowlevel_server  # noqa: SLF001
    orig = lowlevel.lifespan
    box: dict = {}

    @asynccontextmanager
    async def spy(server):
        async with orig(server) as state:
            box["state"] = state
            yield state

    lowlevel.lifespan = spy
    try:
        async with (
            odda_session() as h,
            fixture_site(["index.html", "dialogs.html"]) as fx,
        ):
            bid, tid = await h.open_browser(f"{fx.base}/dialogs.html")
            inst = box["state"].browser._require_instance(bid)  # noqa: SLF001
            page = inst._tabs[tid]  # noqa: SLF001
            cdp = await inst.context.new_cdp_session(page)
            await cdp.send("Page.enable")

            ref = _pluck_ref(await h.page_snapshot(bid, tid), "Confirm me")
            trigger = asyncio.create_task(
                h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})
            )
            await asyncio.wait_for(trigger, 10)

            err = await h.call_error(
                "eval", {"browser_id": bid, "tab_id": tid, "js": "1 + 1"}
            )
            assert err.startswith(f"Tab {tid} has an open confirm dialog")

            await cdp.send("Page.handleJavaScriptDialog", {"accept": True})

            err = await h.call_error(
                "dialog_handle", {"browser_id": bid, "tab_id": tid, "action": "accept"}
            )
            assert err in (
                f"Dialog on tab {tid} was already closed (handled by the user or closed in the browser).",
                f"Tab {tid} in browser {bid} has no open dialog.",
            )

            for attempt in range(200):
                r = await h.client.call_tool(
                    "eval", {"browser_id": bid, "tab_id": tid, "js": "1 + 1"}
                )
                if not r.is_error:
                    assert r.content[0].text == "2"
                    break
                assert "has an open confirm dialog" in r.content[0].text
                assert attempt < 199, (
                    "eval still rejected after the user closed the dialog: "
                    f"{r.content[0].text}"
                )
                await asyncio.sleep(0.05)
            assert "Confirm me" in await h.page_snapshot(bid, tid)
            assert await h.wait_for(
                bid, tid, "window.__confirmResult === true", timeout=5
            )
    finally:
        lowlevel.lifespan = orig


async def test_tabs_close_reports_dialog(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html", "dialogs.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/dialogs.html")
        other = await h.call(
            "tabs_open", {"browser_id": bid, "url": f"{fx.base}/index.html"}
        )
        tid_b = other["tab_id"]

        ref = _pluck_ref(await h.page_snapshot(bid, tid), "Prompt me")
        trigger = asyncio.create_task(
            h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})
        )
        await asyncio.wait_for(trigger, 10)

        r = await h.call("tabs_close", {"browser_id": bid, "tab_id": tid_b})
        assert "closed_dialog" not in r
        assert r["status"] == "closed"

        r = await h.call("tabs_close", {"browser_id": bid, "tab_id": tid})
        assert r["closed_dialog"] == {"type": "prompt", "message": "prompt-message"}


async def test_beforeunload_dialog_on_navigate(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html", "dialogs.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/dialogs.html")

        arm_ref = _pluck_ref(await h.page_snapshot(bid, tid), "Arm beforeunload")
        await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": arm_ref})
        await h.wait_for(bid, tid, "window.__armed === true", timeout=5)

        nav = asyncio.create_task(h.navigate(bid, tid, f"{fx.base}/index.html"))
        r = await asyncio.wait_for(nav, 10)
        d = r["dialog"]
        assert d["type"] == "beforeunload"
        assert d["message"] == ""
        assert d["default_value"] == ""

        await h.call(
            "dialog_handle", {"browser_id": bid, "tab_id": tid, "action": "accept"}
        )
        await h.wait_for(
            bid,
            tid,
            f"location.pathname === '{_site_pathname(fx, 'index.html')}'",
            timeout=5,
        )

        await h.navigate(bid, tid, f"{fx.base}/dialogs.html")
        arm_ref = _pluck_ref(await h.page_snapshot(bid, tid), "Arm beforeunload")
        await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": arm_ref})
        await h.wait_for(bid, tid, "window.__armed === true", timeout=5)
        nav = asyncio.create_task(h.navigate(bid, tid, f"{fx.base}/index.html"))
        r = await asyncio.wait_for(nav, 10)
        assert r["dialog"]["type"] == "beforeunload"
        await h.call(
            "dialog_handle", {"browser_id": bid, "tab_id": tid, "action": "dismiss"}
        )
        await h.wait_for(
            bid,
            tid,
            f"location.pathname === '{_site_pathname(fx, 'dialogs.html')}'",
            timeout=5,
        )


async def test_tabs_open_onload_dialog_returns_details(odda_session) -> None:
    async with (
        odda_session() as h,
        fixture_site(["index.html", "dialogs.html", "onload-dialog.html"]) as fx,
    ):
        bid, tid = await h.open_browser(f"{fx.base}/index.html")

        r = await asyncio.wait_for(
            h.call(
                "tabs_open",
                {"browser_id": bid, "url": f"{fx.base}/onload-dialog.html"},
            ),
            10,
        )
        assert r["dialog"] == {
            "type": "alert",
            "message": "onload-alert",
            "default_value": "",
            "tab_id": tid + 1,
        }
        await h.call(
            "dialog_handle",
            {"browser_id": bid, "tab_id": tid + 1, "action": "accept"},
        )
        await h.wait_for(
            bid,
            tid + 1,
            f"location.pathname === '{_site_pathname(fx, 'onload-dialog.html')}'",
            timeout=5,
        )
