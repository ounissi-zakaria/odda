"""e2e port of scrut 18-proxy-scripts.md: proxy-script install/list/remove.

Proxy-scripts are user-supplied mitmproxy addons odda execs into the
running proxy's addon chain (ADR-0018). Scope is global (one proxy for
all browsers); the name is the key; force gates overwrite. Covers
install (file + source modes), persistence under
``data_dir/proxy-scripts/<name>/script.py``, live-chain firing on real
traffic, remove semantics, validation errors verbatim, capture honesty
(FlowFileAddon ahead of user scripts), runtime hook errors, and the
boot-restore contract (strict xfail: ticket #12 defers restore to the
MCP lifespan).
"""

from __future__ import annotations

import asyncio
import urllib.request
from pathlib import Path

import pytest

from tests.e2e.conftest import odda_session

# --- the scripts under test (mitmproxy -s format: module namespace = addon) ---

PS_MINIMAL = "def response(flow):\n    pass\n"
PS_ADDHEADER = (
    'def response(flow):\n    flow.response.headers["x-odda-proxy-script"] = "fired"\n'
)
PS_REPLACED = (
    "def response(flow):\n"
    '    flow.response.headers["x-odda-proxy-script"] = "replaced"\n'
)
PS_RAISER = 'def response(flow):\n    raise RuntimeError("ps-boom")\n'
PS_INJECT = (
    'def request(flow):\n    flow.request.headers["x-odda-injected"] = "upstream"\n'
)
PS_RESTORE = (
    'def response(flow):\n    flow.response.headers["x-odda-restored"] = "yes"\n'
)


def _script_path(h, name: str) -> Path:
    return h.data_dir / "proxy-scripts" / name / "script.py"


async def _opener(h) -> urllib.request.OpenerDirector:
    """A urllib opener routed through odda's HTTP proxy."""
    proxy_url = await h.call("proxy_url", {})
    return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url}))


async def _fetch(opener, url: str) -> dict[str, str]:
    """Blocking urllib open, run in a thread (no real sleeps)."""

    def _open() -> dict[str, str]:
        with opener.open(url, timeout=10) as resp:
            return {k.lower(): v for k, v in resp.headers.items()}

    return await asyncio.to_thread(_open)


async def _settle() -> None:
    """Let the mitmproxy master finish its startup checkpoints.

    DumpMaster arms mitmproxy's ErrorCheck addon during ``run()``; an
    ERROR-level log (proxy-script exec failure, a raising hook) landing
    before the final startup checkpoint makes the master ``sys.exit(1)``
    ("Error logged during startup"). Yielding the loop a bounded number
    of times lets the already-running ``run()`` task pass its final
    ``shutdown_if_errored`` (which uninstalls the handler) before the
    test fires an error-producing call. Without this the same tests
    race the startup window and fail nondeterministically.
    """
    for _ in range(20):
        await asyncio.sleep(0)


async def test_install_file_persists_source(odda_session, tmp_path) -> None:
    """install --file returns name/size>0 and persists the source on disk."""
    ps = tmp_path / "ps_minimal.py"
    ps.write_text(PS_MINIMAL)
    async with odda_session() as h:
        r = await h.call("proxy_script_install", {"name": "minimal", "file": str(ps)})
        assert r["name"] == "minimal"
        assert r["size"] > 0
        assert _script_path(h, "minimal").is_file()


async def test_install_source_inline(odda_session, tmp_path) -> None:
    """install --source installs inline Python; mutually exclusive with file."""
    async with odda_session() as h:
        r = await h.call(
            "proxy_script_install",
            {"name": "inline", "source": "def response(flow): pass"},
        )
        assert r["name"] == "inline"
        assert r["size"] > 0
        assert _script_path(h, "inline").is_file()


async def test_list_returns_installed_sorted(odda_session, tmp_path) -> None:
    """list returns both installed scripts, names sorted."""
    ps = tmp_path / "ps_minimal.py"
    ps.write_text(PS_MINIMAL)
    async with odda_session() as h:
        await h.call("proxy_script_install", {"name": "minimal", "file": str(ps)})
        await h.call(
            "proxy_script_install",
            {"name": "inline", "source": "def response(flow): pass"},
        )
        r = await h.call("proxy_script_list", {})
        assert sorted(s["name"] for s in r) == ["inline", "minimal"]


async def test_remove_deletes_script_and_stops_hook(odda_session, tmp_path) -> None:
    """remove returns name/removed, deletes the dir, stops the hook firing."""
    from tests.e2e.conftest import fixture_site

    ps = tmp_path / "ps_addheader.py"
    ps.write_text(PS_ADDHEADER)
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        await h.call("proxy_script_install", {"name": "addheader", "file": str(ps)})
        opener = await _opener(h)

        # hook fires on live traffic through the proxy
        hdrs = await _fetch(opener, f"{fx.base}/?marker=ps-fires")
        assert hdrs.get("x-odda-proxy-script") == "fired"

        r = await h.call("proxy_script_remove", {"name": "addheader"})
        assert r == {"name": "addheader", "removed": True}
        assert not _script_path(h, "addheader").parent.exists()

        # hook no longer fires
        hdrs = await _fetch(opener, f"{fx.base}/?marker=ps-stopped")
        assert "x-odda-proxy-script" not in hdrs

        # the earlier flow was still captured (script ran after FlowFileAddon)
        await h.wait_flow("ps-fires")


async def test_collision_and_force_swap(odda_session, tmp_path) -> None:
    """install on an existing name errors; force swaps the live chain."""
    from tests.e2e.conftest import fixture_site

    ps = tmp_path / "ps_addheader.py"
    ps2 = tmp_path / "ps_replaced.py"
    ps.write_text(PS_ADDHEADER)
    ps2.write_text(PS_REPLACED)
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        await h.call("proxy_script_install", {"name": "minimal", "source": PS_MINIMAL})
        err = await h.call_error(
            "proxy_script_install",
            {"name": "minimal", "source": "def response(flow): pass"},
        )
        assert "already installed" in err

        # fresh install, then force-overwrite with a different header value
        await h.call("proxy_script_install", {"name": "addheader", "file": str(ps)})
        r = await h.call(
            "proxy_script_install",
            {"name": "addheader", "file": str(ps2), "force": True},
        )
        assert r["name"] == "addheader"
        # on-disk source is the new one
        assert "replaced" in _script_path(h, "addheader").read_text()

        opener = await _opener(h)
        hdrs = await _fetch(opener, f"{fx.base}/?marker=ps-replaced")
        assert hdrs.get("x-odda-proxy-script") == "replaced"


async def test_validation_errors_verbatim(odda_session, tmp_path) -> None:
    """both/neither/missing-file/empty-source/remove-missing all error verbatim."""
    ps = tmp_path / "ps_minimal.py"
    ps.write_text(PS_MINIMAL)
    async with odda_session() as h:
        err = await h.call_error(
            "proxy_script_install",
            {"name": "both", "file": str(ps), "source": "1"},
        )
        assert "Provide either file or source, not both" in err

        err = await h.call_error("proxy_script_install", {"name": "neither"})
        assert "Provide file <path> or source <py>" in err

        err = await h.call_error(
            "proxy_script_install",
            {"name": "missing", "file": str(tmp_path / "does-not-exist.py")},
        )

        err = await h.call_error(
            "proxy_script_install", {"name": "empty", "source": "   "}
        )
        assert "source is empty" in err

        err = await h.call_error("proxy_script_remove", {"name": "nope"})
        assert "Proxy-script 'nope' not found" in err


async def test_exec_failure_errors(odda_session, tmp_path) -> None:
    """A non-Python source fails to exec with the library's message."""
    bad = tmp_path / "ps_bad.py"
    bad.write_text("this is not python {{{")
    async with odda_session() as h:
        await _settle()
        err = await h.call_error(
            "proxy_script_install", {"name": "bad", "file": str(bad), "force": True}
        )
        assert "Failed to exec proxy-script 'bad'" in err


async def test_runtime_hook_error_does_not_crash_proxy(odda_session, tmp_path) -> None:
    """A raising response hook: proxy still serves and captures afterwards.

    The scrut doc's `odda logs` log-grep is dead surface (logs command
    deleted); the surviving contract is the proxy keeps serving and
    capturing after the hook error.
    """
    from tests.e2e.conftest import curl, fixture_site

    ps = tmp_path / "ps_raise.py"
    ps.write_text(PS_RAISER)
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        await _settle()
        await h.call("proxy_script_install", {"name": "raiser", "file": str(ps)})
        proxy = await h.call("proxy_url", {})

        cp = await curl(
            f"{fx.base}/?marker=ps-boom",
            proxy,
            extra=["-o", "/dev/null", "-w", "curl_status=%{http_code}"],
        )
        assert "curl_status=200" in cp.stdout

        # the proxy still serves and captures a subsequent request
        cp = await curl(
            f"{fx.base}/?marker=ps-after-boom",
            proxy,
            extra=["-o", "/dev/null", "-w", "curl_status=%{http_code}"],
        )
        assert "curl_status=200" in cp.stdout
        await h.wait_flow("ps-after-boom")


async def test_capture_honesty_flowfile_ahead(odda_session, tmp_path) -> None:
    """FlowFileAddon records the original request; mutations reach upstream.

    A request hook injecting a header mutates what goes upstream, but the
    captured flow's request file must NOT contain it (ADR-0018 ordering).
    """
    from tests.e2e.conftest import fixture_site

    ps = tmp_path / "ps_inject.py"
    ps.write_text(PS_INJECT)
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        await h.call("proxy_script_install", {"name": "injector", "file": str(ps)})
        opener = await _opener(h)
        await _fetch(opener, f"{fx.base}/?marker=ps-capture-honesty")

        rec = await h.wait_flow("ps-capture-honesty")
        req = (h.data_dir / "flows" / rec["id"] / "request").read_bytes()
        assert b"x-odda-injected" not in req.lower()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "ticket #12: proxy-script restore on MCP lifespan boot is deferred; "
        "this test flips to green when it lands"
    ),
)
async def test_boot_restore_readds_persisted_script(odda_session, tmp_path) -> None:
    """A persisted proxy-script is re-added on server boot (ticket #12).

    Install a script, exit the session, start a new session on the SAME
    data dir — the boot scan must re-exec and re-add it, observable both
    in proxy_script_list and in the hook firing on live traffic.
    """
    from tests.e2e.conftest import fixture_site

    ps = tmp_path / "ps_restore.py"
    ps.write_text(PS_RESTORE)
    async with fixture_site(["index.html"]) as fx:
        async with odda_session() as h:
            await h.call("proxy_script_install", {"name": "restorer", "file": str(ps)})

        # Fresh session on the same tmp_path → same .odda data dir.
        async with odda_session() as h:
            r = await h.call("proxy_script_list", {})
            assert "restorer" in {s["name"] for s in r}

            opener = await _opener(h)
            hdrs = await _fetch(opener, f"{fx.base}/?marker=ps-restored")
            assert hdrs.get("x-odda-restored") == "yes"
