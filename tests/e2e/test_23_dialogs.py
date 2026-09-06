"""Dialog-blocking engine e2e tests (ADR-0022 through the MCP client).

Native dialogs (alert/confirm/prompt/beforeunload) block the page
until handled. The tool that opens one returns its details immediately
(trigger rule); while one is open, every other tool on that tab rejects
with an anticipated error naming the dialog (drop, not park — the
agent handles it via dialog_handle and retries).
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager

import odda.mcp as odda_mcp
from tests.e2e.conftest import fixture_site, odda_session


def _pluck_ref(snapshot: str, label: str) -> str:
    """Return the aria-ref for the line whose label matches.

    Refs carry a document-generation prefix that changes across
    navigations (e2, then f2e5, ...), so match any token.
    """
    line = next(l for l in snapshot.splitlines() if label in l)
    return re.search(r"\[ref=(\w+)\]", line).group(1)


async def test_trigger_rule_three_tools(odda_session) -> None:
    """page_click, eval and wait_for all resolve promptly with the open
    dialog's details instead of hanging behind the frozen renderer."""
    async with odda_session() as h, fixture_site(["index.html", "dialogs.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/dialogs.html")

        # page_click on a confirm button
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

        # eval opening alert() mid-script
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
        # wait_for whose expression opens a confirm: the dialog
        # listener wins the race, the tool returns the dialog
        # details, and the parked poll dies on its own short timeout.
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
    """While a dialog is open, same-tab eval and page_snapshot reject
    with the anticipated dialog error; after dialog_handle the same
    calls succeed."""
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
    """accept/dismiss answer confirm() true/false; prompt prompt_text
    lands; accept without prompt_text uses the default_value; dismiss
    answers null; a no-dialog tab gets the anticipated error."""
    async with odda_session() as h, fixture_site(["index.html", "dialogs.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/dialogs.html")
        await h.eval(bid, tid, "window.name = ''")

        # accept confirm → true
        ref = _pluck_ref(await h.page_snapshot(bid, tid), "Confirm me")
        await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})
        r = await h.call(
            "dialog_handle", {"browser_id": bid, "tab_id": tid, "action": "accept"}
        )
        assert r == {"handled": True, "action": "accept", "tab_id": tid}
        assert await h.wait_for(bid, tid, "window.__confirmResult === true", timeout=5)

        # dismiss confirm → false
        await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})
        r = await h.call(
            "dialog_handle", {"browser_id": bid, "tab_id": tid, "action": "dismiss"}
        )
        assert r == {"handled": True, "action": "dismiss", "tab_id": tid}
        assert await h.wait_for(bid, tid, "window.__confirmResult === false", timeout=5)

        # prompt + prompt_text → the text lands
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

        # prompt accept without prompt_text → the default_value lands
        await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})
        await h.call(
            "dialog_handle", {"browser_id": bid, "tab_id": tid, "action": "accept"}
        )
        assert await h.wait_for(
            bid, tid, "window.__promptResult === 'prefilled'", timeout=5
        )

        # prompt dismiss → null
        await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})
        await h.call(
            "dialog_handle", {"browser_id": bid, "tab_id": tid, "action": "dismiss"}
        )
        assert await h.wait_for(bid, tid, "window.__promptResult === null", timeout=5)

        # no open dialog → anticipated error
        err = await h.call_error(
            "dialog_handle", {"browser_id": bid, "tab_id": tid, "action": "accept"}
        )
        assert err == f"Tab {tid} in browser {bid} has no open dialog."


async def test_user_close_clears_and_unblocks(odda_session) -> None:
    """A browser-level close (raw CDP accept) clears the dialog state:
    previously rejected calls succeed, the page records the answer,
    and a late dialog_handle gets the already-closed error."""
    # Issue 04 item 5 sanctions this headless-safe user-close
    # simulation: raw CDP outside the driver. The internals touched
    # here are the *mechanism* only — every assertion below stays
    # on MCP-client-observable results.
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
            # Chrome only tracks dialogs for CDP sessions that were
            # enabled before the dialog opened; arm the session first.
            cdp = await inst.context.new_cdp_session(page)
            await cdp.send("Page.enable")

            ref = _pluck_ref(await h.page_snapshot(bid, tid), "Confirm me")
            trigger = asyncio.create_task(
                h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": ref})
            )
            await asyncio.wait_for(trigger, 10)

            # The reject proves the dialog is open from the tool side.
            err = await h.call_error(
                "eval", {"browser_id": bid, "tab_id": tid, "js": "1 + 1"}
            )
            assert err.startswith(f"Tab {tid} has an open confirm dialog")

            await cdp.send("Page.handleJavaScriptDialog", {"accept": True})

            # In-proc dispatch beats the probe's pipe round-trip: the
            # entry-present branch dominates, but both branches are the
            # anticipated-error contract; assert membership, not a winner.
            err = await h.call_error(
                "dialog_handle", {"browser_id": bid, "tab_id": tid, "action": "accept"}
            )
            assert err in (
                f"Dialog on tab {tid} was already closed (handled by the user or closed in the browser).",
                f"Tab {tid} in browser {bid} has no open dialog.",
            )

            # The rejected call is now a plain success: retry runs it.
            assert await h.eval(bid, tid, "1 + 1") == "2"
            assert "Confirm me" in await h.page_snapshot(bid, tid)
            assert await h.wait_for(
                bid, tid, "window.__confirmResult === true", timeout=5
            )
    finally:
        lowlevel.lifespan = orig


async def test_tabs_close_reports_dialog(odda_session) -> None:
    """Closing the dialog tab reports closed_dialog {type, message};
    closing a non-dialog tab carries no such key."""
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

        # non-dialog tab: no closed_dialog key (the dialog lives
        # on the other tab, untouched by this close)
        r = await h.call("tabs_close", {"browser_id": bid, "tab_id": tid_b})
        assert "closed_dialog" not in r
        assert r["status"] == "closed"

        # dialog tab: close destroys the dialog, reported pre-close
        r = await h.call("tabs_close", {"browser_id": bid, "tab_id": tid})
        assert r["closed_dialog"] == {"type": "prompt", "message": "prompt-message"}


async def test_beforeunload_dialog_on_navigate(odda_session) -> None:
    """Armed with a trusted click, navigating away opens the
    beforeunload dialog and the navigate tool returns its details
    promptly (no 30s hang); accept completes the navigation and
    dismiss leaves the page in place."""
    async with odda_session() as h, fixture_site(["index.html", "dialogs.html"]) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/dialogs.html")

        arm_ref = _pluck_ref(await h.page_snapshot(bid, tid), "Arm beforeunload")
        await h.call("page_click", {"browser_id": bid, "tab_id": tid, "ref": arm_ref})
        await h.wait_for(bid, tid, "window.__armed === true", timeout=5)

        nav = asyncio.create_task(h.navigate(bid, tid, f"{fx.base}/index.html"))
        r = await asyncio.wait_for(nav, 10)
        d = r["dialog"]
        assert d["type"] == "beforeunload"
        # Headless Chrome reports the beforeunload dialog with no
        # message text; the pin's trigger rule carries the empties.
        assert d["message"] == ""
        assert d["default_value"] == ""

        await h.call(
            "dialog_handle", {"browser_id": bid, "tab_id": tid, "action": "accept"}
        )
        await h.wait_for(bid, tid, "location.pathname === '/index.html'", timeout=5)

        # dismiss: navigation stays. Reload dialogs.html first (the
        # page navigated away on accept) and re-arm before trying again.
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
        await h.wait_for(bid, tid, "location.pathname === '/dialogs.html'", timeout=5)


async def test_tabs_open_onload_dialog_returns_details(odda_session) -> None:
    """tabs_open to a page that opens a dialog at load returns the
    dialog's details promptly instead of hanging behind the frozen
    renderer (the same trigger rule as navigate); dialog_handle
    completes the open in the background."""
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
        # The parked goto finished; the new tab is at the target URL.
        await h.wait_for(
            bid, tid + 1, "location.pathname === '/onload-dialog.html'", timeout=5
        )
