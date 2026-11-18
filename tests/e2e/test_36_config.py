# See ADR-0042.
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from odda.config import ConfigError, OddaConfig, load_config


def _seed_config(tmp_path: Path, content: str) -> None:
    (tmp_path / ".odda").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".odda" / "config.toml").write_text(content)


def test_defaults_when_no_config_exists(tmp_path: Path) -> None:
    cfg = load_config(
        global_path=tmp_path / "global.toml",
        project_path=tmp_path / "project.toml",
    )
    assert cfg == OddaConfig(
        max_browsers=3, stale_browser_timeout=0, vnc_bind="127.0.0.1"
    )


def test_project_overrides_global(tmp_path: Path) -> None:
    global_toml = tmp_path / "global.toml"
    global_toml.write_text('max_browsers = 7\nvnc_bind = "10.0.0.1"\n')
    project_toml = tmp_path / "project.toml"
    project_toml.write_text("max_browsers = 2\n")
    cfg = load_config(global_path=global_toml, project_path=project_toml)
    assert cfg == OddaConfig(
        max_browsers=2, stale_browser_timeout=0, vnc_bind="10.0.0.1"
    )


def test_cli_overrides_files(tmp_path: Path) -> None:
    project_toml = tmp_path / "project.toml"
    project_toml.write_text('vnc_bind = "10.0.0.1"\nstale_browser_timeout = 60\n')
    cfg = load_config(
        {"vnc_bind": "192.168.1.5", "stale_browser_timeout": 0},
        global_path=tmp_path / "global.toml",
        project_path=project_toml,
    )
    assert cfg == OddaConfig(
        max_browsers=3, stale_browser_timeout=0, vnc_bind="192.168.1.5"
    )


def test_global_default_path_is_config_home(tmp_path: Path, config_home: Path) -> None:
    (config_home / "config.toml").write_text("max_browsers = 9\n")
    cfg = load_config(project_path=tmp_path / "none.toml")
    assert cfg.max_browsers == 9


def test_unknown_key_fails_naming_file_and_key(tmp_path: Path) -> None:
    project_toml = tmp_path / "project.toml"
    project_toml.write_text("max_browser = 2\n")
    with pytest.raises(ConfigError) as ei:
        load_config(global_path=tmp_path / "none.toml", project_path=project_toml)
    message = str(ei.value)
    assert str(project_toml) in message
    assert "max_browser" in message
    assert "max_browsers" in message


def test_invalid_toml_fails_naming_file(tmp_path: Path) -> None:
    project_toml = tmp_path / "project.toml"
    project_toml.write_text("max_browsers = \n")
    with pytest.raises(ConfigError) as ei:
        load_config(global_path=tmp_path / "none.toml", project_path=project_toml)
    assert str(project_toml) in str(ei.value)


def test_bad_values_fail_naming_file_key_and_value(tmp_path: Path) -> None:
    bad = [
        ("max_browsers = -1\n", "max_browsers"),
        ('max_browsers = "lots"\n', "max_browsers"),
        ("stale_browser_timeout = true\n", "stale_browser_timeout"),
        ('vnc_bind = "localhost"\n', "vnc_bind"),
    ]
    for content, key in bad:
        project_toml = tmp_path / "project.toml"
        project_toml.write_text(content)
        with pytest.raises(ConfigError) as ei:
            load_config(global_path=tmp_path / "none.toml", project_path=project_toml)
        message = str(ei.value)
        assert str(project_toml) in message, content
        assert key in message, content


def test_cli_override_error_names_the_flag(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as ei:
        load_config(
            {"vnc_bind": "localhost"},
            global_path=tmp_path / "none.toml",
            project_path=tmp_path / "none.toml",
        )
    assert "--vnc-bind" in str(ei.value)


def test_none_override_contributes_nothing() -> None:
    assert load_config({"max_browsers": None, "vnc_bind": None}) == OddaConfig()


async def test_max_browsers_blocks_and_recovers(tmp_path: Path, odda_session) -> None:
    _seed_config(tmp_path, "max_browsers = 1\n")
    async with odda_session() as h:
        bid, _ = await h.open_browser()
        error = await h.call_error("browser_open", {})
        assert "Browser limit reached (1 open)" in error
        await h.call("browser_close", {"browser_id": bid})
        bid2, _ = await h.open_browser()
        assert bid2 != bid


async def test_max_browsers_zero_is_unlimited(tmp_path: Path, odda_session) -> None:
    _seed_config(tmp_path, "max_browsers = 0\n")
    async with odda_session() as h:
        bid, _ = await h.open_browser()
        bid2, _ = await h.open_browser()
        assert {bid, bid2} == {
            r["browser_id"]
            for r in await h.call("browser_list", {})
            if r["state"] == "open"
        }


async def _states(h) -> dict[str, str]:
    rows = await h.call("browser_list", {})
    return {r["browser_id"]: r["state"] for r in rows}


async def _wait_closed(h, browser_id: str, deadline_s: float) -> str:
    end = time.monotonic() + deadline_s
    states: dict[str, str] = {}
    while time.monotonic() < end:
        states = await _states(h)
        if states.get(browser_id) == "closed":
            return "closed"
        await asyncio.sleep(0.25)
    return states.get(browser_id, "?")


async def test_stale_browser_auto_closes(tmp_path: Path, odda_session) -> None:
    _seed_config(tmp_path, "stale_browser_timeout = 1\n")
    async with odda_session() as h:
        bid, _ = await h.open_browser()
        assert await _wait_closed(h, bid, 6) == "closed"


async def test_stale_close_spares_the_touched_browser(
    tmp_path: Path, odda_session
) -> None:
    _seed_config(tmp_path, "stale_browser_timeout = 10\n")
    async with odda_session() as h:
        touched, _ = await h.open_browser()
        idle, _ = await h.open_browser()
        end = time.monotonic() + 18
        while time.monotonic() < end:
            await h.call("tabs_list", {"browser_id": touched})
            if (await _states(h)).get(idle) == "closed":
                break
            await asyncio.sleep(0.4)
        states = await _states(h)
        assert states[idle] == "closed"
        assert states[touched] == "open"


async def test_vnc_bind_rides_config(tmp_path: Path, odda_session) -> None:
    _seed_config(tmp_path, 'vnc_bind = "127.0.0.2"\n')
    async with odda_session() as h:
        bid, _ = await h.open_browser()
        row = next(
            r for r in await h.call("browser_list", {}) if r["browser_id"] == bid
        )
        assert row["novnc_url"].startswith("http://127.0.0.2:")
        assert row["vnc_url"].startswith("vnc://127.0.0.1:")


def test_cli_flags_parse() -> None:
    from odda.cli import _build_parser

    args = _build_parser().parse_args(
        [
            "mcp",
            "--max-browsers",
            "5",
            "--stale-browser-timeout",
            "30",
            "--vnc-bind",
            "127.0.0.2",
        ]
    )
    assert (args.max_browsers, args.stale_browser_timeout, args.vnc_bind) == (
        5,
        30,
        "127.0.0.2",
    )


async def test_cli_flag_overrides_project_config(
    tmp_path: Path, config_home: Path
) -> None:
    import sys

    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    from tests.e2e.conftest import Harness

    _seed_config(tmp_path, "max_browsers = 2\n")
    src = Path(__file__).resolve().parents[2] / "src"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "odda", "mcp", "--max-browsers", "1"],
        env={"PYTHONPATH": str(src), "ODDA_CONFIG_HOME": str(config_home)},
        cwd=tmp_path,
    )
    async with Client(params) as client:
        h = Harness(client, tmp_path / ".odda", config_home)
        bid, _ = await h.open_browser()
        error = await h.call_error("browser_open", {})
        assert "Browser limit reached (1 open)" in error
        await h.call("browser_close", {"browser_id": bid})
        bid2, _ = await h.open_browser()
        assert bid2 != bid


async def test_stale_close_spares_an_in_flight_call(
    tmp_path: Path, odda_session
) -> None:
    _seed_config(tmp_path, "stale_browser_timeout = 5\n")
    async with odda_session() as h:
        bid, tid = await h.open_browser()
        error = await h.call_error(
            "wait_for",
            {"browser_id": bid, "tab_id": tid, "expression": "0", "timeout": 12},
        )
        assert "Timeout after 12000.0ms" in error
        assert (await _states(h))[bid] == "open"
        assert await _wait_closed(h, bid, 10) == "closed"
