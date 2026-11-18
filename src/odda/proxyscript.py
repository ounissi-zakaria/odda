# See ADR-0018.
from __future__ import annotations

import logging
import shutil
from typing import TYPE_CHECKING, Any

from mitmproxy import hooks
from mitmproxy.addons.script import load_script

from odda import flowstore

if TYPE_CHECKING:
    from pathlib import Path

    from mitmproxy.master import Master

logger = logging.getLogger(__name__)

PROXY_SCRIPTS_DIR_NAME = "proxy-scripts"
SCRIPT_FILENAME = "script.py"


def _proxy_scripts_dir() -> Path:
    return flowstore.DATA_DIR / PROXY_SCRIPTS_DIR_NAME


def _script_path(name: str) -> Path:
    return _proxy_scripts_dir() / name / SCRIPT_FILENAME


def install_source(name: str, source: str) -> Path:
    path = _script_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def remove_source(name: str) -> None:
    d = _proxy_scripts_dir() / name
    if not d.exists():
        msg = f"Proxy-script '{name}' not found"
        raise ValueError(msg)
    shutil.rmtree(d)


def list_scripts() -> list[dict[str, Any]]:
    d = _proxy_scripts_dir()
    if not d.exists():
        return []
    scripts: list[dict[str, Any]] = []
    for entry in sorted(d.iterdir()):
        if not entry.is_dir():
            continue
        script_file = entry / SCRIPT_FILENAME
        if script_file.exists():
            scripts.append({"name": entry.name, "size": script_file.stat().st_size})
    return scripts


def name_exists(name: str) -> bool:
    return _script_path(name).exists()


class ProxyScriptManager:
    def __init__(self, master: Master) -> None:
        self._master = master
        self._live: dict[str, Any] = {}

    def install(self, name: str, source: str) -> dict[str, Any]:
        self._remove_live(name)

        path = install_source(name, source)
        ns = self._load_and_add(path)
        if ns is None:
            msg = f"Failed to exec proxy-script '{name}' (see server log)"
            raise ValueError(msg)
        self._live[name] = ns
        return {"name": name, "size": path.stat().st_size}

    def remove(self, name: str) -> dict[str, Any]:
        self._remove_live(name)
        remove_source(name)
        return {"name": name, "removed": True}

    def list_live(self) -> list[dict[str, Any]]:
        # See ADR-0018.
        return list_scripts()

    def restore_on_boot(self) -> None:
        # See ADR-0017.
        d = _proxy_scripts_dir()
        if not d.exists():
            return
        for entry in sorted(d.iterdir()):
            if not entry.is_dir():
                continue
            script_file = entry / SCRIPT_FILENAME
            if not script_file.exists():
                continue
            ns = self._load_and_add(script_file)
            if ns is None:
                logger.error(
                    "Skipping proxy-script '%s' on boot (exec failed; "
                    "see preceding error)",
                    entry.name,
                )
                continue
            self._live[entry.name] = ns
            logger.info("Restored proxy-script '%s'", entry.name)

    def _load_and_add(self, path: Path) -> Any:
        ns = load_script(str(path))
        if ns is None:
            return None
        self._master.addons.add(ns)
        self._master.addons.invoke_addon_sync(
            ns, hooks.ConfigureHook(self._master.options.keys())
        )
        self._master.addons.invoke_addon_sync(ns, hooks.RunningHook())
        return ns

    def _remove_live(self, name: str) -> None:
        ns = self._live.pop(name, None)
        if ns is not None:
            self._master.addons.remove(ns)
