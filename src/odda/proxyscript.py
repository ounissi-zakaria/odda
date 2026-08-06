"""Proxy-script storage and live addon management.

A proxy-script is a Python file in mitmproxy ``-s`` script format that
odda execs and adds to the running proxy's addon chain. The file's module
namespace is the addon (top-level ``request``/``response``/``load``/
``running``/... hooks, or an ``addons = [...]`` list for composition) —
exactly what works with ``mitmproxy -s addon.py``. No odda-specific
wrapper.

Storage mirrors userscripts: source is persisted under
``<DATA_DIR>/proxy-scripts/<name>/script.py`` and re-added on server boot.
Scope is global (one proxy shared across all browsers), not per-browser
like userscripts — see ADR-0018.

``--name`` is odda's key. ``install`` refuses without ``--force`` if the
name is taken; with ``--force`` odda removes the existing instance first,
so mitmproxy's duplicate-``.name`` ``AddonManagerError`` never surfaces.
After ``addons.add`` odda fires ``RunningHook`` itself (the master is
already running when install is called mid-session), mirroring
mitmproxy's own ``ScriptLoader.configure``.

A failing exec (syntax error, import error) is logged and the install is
rejected — one bad proxy-script never blocks the server. Runtime hook
errors are swallowed by mitmproxy's ``safecall()`` and surface via
``odda logs``.
"""

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
    """Return the proxy-scripts dir under the data dir (not created here)."""
    return flowstore.DATA_DIR / PROXY_SCRIPTS_DIR_NAME


def _script_path(name: str) -> Path:
    """Return the on-disk script path for a proxy-script named ``name``."""
    return _proxy_scripts_dir() / name / SCRIPT_FILENAME


def install_source(name: str, source: str) -> Path:
    """Persist ``source`` as the script for ``name``, overwriting any existing file.

    Creates the proxy-scripts dir and the per-name dir lazily. This only
    writes the file; the live addon is added separately via
    :func:`_load_and_add`. Kept as a distinct step so the boot scan and
    ``--force`` reinstall share one write path.
    """
    path = _script_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def remove_source(name: str) -> None:
    """Delete the on-disk script dir for ``name``.

    Raises:
        ValueError: If the proxy-script is not on disk.
    """
    d = _proxy_scripts_dir() / name
    if not d.exists():
        msg = f"Proxy-script '{name}' not found"
        raise ValueError(msg)
    shutil.rmtree(d)


def list_scripts() -> list[dict[str, Any]]:
    """List all persisted proxy-scripts from disk."""
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
    """Return True if a proxy-script named ``name`` is persisted on disk."""
    return _script_path(name).exists()


class ProxyScriptManager:
    """Manages the live addon chain for proxy-scripts.

    Holds a reference to the mitmproxy ``Master`` (via :class:`ProxyServer`)
    and tracks the exec'd module objects by odda name so ``remove`` and
    ``--force`` reinstall can take the live instance out of the chain.
    """

    def __init__(self, master: Master) -> None:
        """Bind to a running mitmproxy master.

        Args:
            master: The ``DumpMaster`` from :class:`ProxyServer`. Must have
                its ``addons`` manager ready (it is, after ``DumpMaster.__init__``).
        """
        self._master = master
        self._live: dict[str, Any] = {}

    def install(self, name: str, source: str) -> dict[str, Any]:
        """Persist, exec, and add a proxy-script under ``name``.

        Overwrites an existing proxy-script of the same name: removes the
        live instance first (firing its ``done``), then execs the new
        source and adds it. This is the ``--force`` path; the caller
        gates whether overwrite is allowed.

        Returns:
            ``{"name": ..., "size": N}``.

        Raises:
            ValueError: If exec fails (syntax/import error) or the module
                has no addon hooks. The persisted file is left on disk so
                the boot scan can retry on next server start.
        """
        # Remove the live instance first if present, so mitmproxy's
        # duplicate-.name AddonManagerError can never fire.
        self._remove_live(name)

        path = install_source(name, source)
        ns = self._load_and_add(path)
        if ns is None:
            msg = f"Failed to exec proxy-script '{name}' (see server log)"
            raise ValueError(msg)
        self._live[name] = ns
        return {"name": name, "size": path.stat().st_size}

    def remove(self, name: str) -> dict[str, Any]:
        """Remove a proxy-script: live instance + on-disk source.

        Raises:
            ValueError: If the proxy-script is not on disk.
        """
        self._remove_live(name)
        remove_source(name)
        return {"name": name, "removed": True}

    def list_live(self) -> list[dict[str, Any]]:
        """List proxy-scripts from disk.

        Disk is the source of truth for ``list`` (matches userscripts).
        A proxy-script whose exec failed at boot is on disk yet not
        live; the agent discovers that via ``odda logs`` (runtime errors
        surface there per ADR-0018), not via a flag here — list carries
        no error state, mirroring userscript's list.
        """
        return list_scripts()

    def restore_on_boot(self) -> None:
        """Re-exec and add every persisted proxy-script.

        Called during server boot. A proxy-script that fails to exec is
        logged and skipped (mirrors userscripts' try/catch isolation);
        the rest of the chain comes up. Order is alphabetical by name for
        determinism. No-op when the proxy-scripts dir doesn't exist yet
        (lazy data dir — ADR-0017).
        """
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
        """Exec a script file and add the resulting module to the chain.

        Fires ``ConfigureHook`` then ``RunningHook`` after ``add``,
        because the master is already running by the time install or the
        boot scan is called. This mirrors mitmproxy's own
        ``ScriptLoader.configure``, which does
        ``ConfigureHook(ctx.options.keys())`` then ``RunningHook()`` when
        a script is loaded into an already-running master — so an addon
        that registers options in ``load`` gets its ``configure`` hook
        and can read them. Returns the module namespace, or ``None`` if
        exec failed (``load_script`` logs the failure itself and returns
        ``None``).
        """
        ns = load_script(str(path))
        if ns is None:
            return None
        self._master.addons.add(ns)
        # The master is already running; mitmproxy's own ScriptLoader
        # does the same (configure(): "if self.is_running: ConfigureHook
        # then RunningHook").
        self._master.addons.invoke_addon_sync(
            ns, hooks.ConfigureHook(self._master.options.keys())
        )
        self._master.addons.invoke_addon_sync(ns, hooks.RunningHook())
        return ns

    def _remove_live(self, name: str) -> None:
        """Remove the live addon instance for ``name`` if present (fires done)."""
        ns = self._live.pop(name, None)
        if ns is not None:
            self._master.addons.remove(ns)
