"""Userscript storage: JS helpers that auto-run at document_start via extension.

Userscripts are stored per-browser on disk under
``<DATA_DIR>/browsers/<browser_id>/userscripts/<name>/script.js``. Each
browser gets its own Chrome extension at
``<DATA_DIR>/browsers/<browser_id>/userscripts-extension/`` with a
``content.js`` that inlines all installed userscripts for that browser
(each wrapped in try/catch), plus the set of built-in default
userscripts shipped with odda. The extension is loaded via CDP
``Extensions.loadUnpacked`` and runs at ``document_start`` in the
``MAIN`` world, so ``window`` modifications are visible to the page and
to ``eval``.

Per ADR-0010, the scope is per-browser: a userscript installed on
browser 1 does not reach browser 2. The ``browser_id`` on
install/remove is the scope key, not just a reload trigger.
"""

from __future__ import annotations

import shutil
from importlib import resources
from typing import TYPE_CHECKING, Any

from odda import flowstore

if TYPE_CHECKING:
    from pathlib import Path

BROWSERS_DIR_NAME = "browsers"
USERSCRIPTS_DIR_NAME = "userscripts"
USERSCRIPTS_EXTENSION_DIR_NAME = "userscripts-extension"
SCRIPT_FILENAME = "script.js"
DEFAULT_USERSCRIPTS_PACKAGE = "odda.userscripts.default"

MANIFEST_JSON = """{
  "manifest_version": 3,
  "name": "odda-userscripts",
  "version": "1.0",
  "content_scripts": [{
    "matches": ["<all_urls>"],
    "js": ["content.js"],
    "run_at": "document_start",
    "world": "MAIN",
    "all_frames": true
  }]
}
"""


def browsers_root() -> Path:
    """Return the root dir holding per-browser data dirs (ADR-0030)."""
    return flowstore.DATA_DIR / BROWSERS_DIR_NAME


def _browser_dir(browser_id: str) -> Path:
    """Return the per-browser data dir for ``browser_id``."""
    return browsers_root() / browser_id


def userscripts_dir(browser_id: str) -> Path:
    """Return the per-browser userscripts dir for ``browser_id``."""
    return _browser_dir(browser_id) / USERSCRIPTS_DIR_NAME


def extension_dir(browser_id: str) -> Path:
    """Return the per-browser userscript extension dir for ``browser_id``."""
    return _browser_dir(browser_id) / USERSCRIPTS_EXTENSION_DIR_NAME


def install(browser_id: str, name: str, source: str) -> dict[str, Any]:
    """Install a userscript, overwriting any existing one, and resync the extension.

    Args:
        browser_id: Browser whose scope to install into.
        name: Userscript name.
        source: JavaScript source.
    """
    script_dir = userscripts_dir(browser_id) / name
    if script_dir.exists():
        shutil.rmtree(script_dir)
    script_dir.mkdir(parents=True, exist_ok=True)
    (script_dir / SCRIPT_FILENAME).write_text(source, encoding="utf-8")
    sync_extension(browser_id)
    return {"name": name, "size": len(source)}


def remove(browser_id: str, name: str) -> dict[str, Any]:
    """Remove a userscript and resync the extension.

    Args:
        browser_id: Browser whose scope to remove from.
        name: Userscript name.

    Raises:
        ValueError: If the script does not exist.
    """
    script_dir = userscripts_dir(browser_id) / name
    if not script_dir.exists():
        msg = f"Userscript '{name}' not found"
        raise ValueError(msg)
    shutil.rmtree(script_dir)
    sync_extension(browser_id)
    return {"name": name, "removed": True}


def list_scripts(browser_id: str) -> list[dict[str, Any]]:
    """List all installed userscripts for one browser from disk."""
    d = userscripts_dir(browser_id)
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


def _read_default_scripts() -> list[tuple[str, str]]:
    """Load built-in default userscripts packaged with odda.

    Returns:
        A list of (name, source) tuples for each ``*.js`` file in the
        ``odda.userscripts.default`` package.
    """
    return [
        (entry.name, entry.read_text(encoding="utf-8"))
        for entry in sorted(resources.files(DEFAULT_USERSCRIPTS_PACKAGE).iterdir())
        if entry.is_file() and entry.name.endswith(".js")
    ]


def sync_extension(browser_id: str) -> Path:
    """Regenerate one browser's extension from installed and default userscripts.

    Default userscripts are inlined first so they establish globals before any
    user-installed script runs. Each script is wrapped in try/catch so one
    failing script doesn't break the rest. Writes manifest.json if missing.
    Returns the extension dir path.
    """
    ext_dir = extension_dir(browser_id)
    ext_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = ext_dir / "manifest.json"
    if not manifest_path.exists():
        manifest_path.write_text(MANIFEST_JSON, encoding="utf-8")

    parts: list[str] = ["// Auto-generated by odda. Do not edit.\n"]

    for name, source in _read_default_scripts():
        parts.append(
            f"// === default userscript: {name} ===\n"
            f"try {{\n{source}\n}} catch (e) {{\n"
            f"  console.error('[odda] default userscript {name} failed:', e);\n"
            f"}}\n"
        )

    scripts_dir = userscripts_dir(browser_id)
    if scripts_dir.exists():
        for entry in sorted(scripts_dir.iterdir()):
            if not entry.is_dir():
                continue
            script_file = entry / SCRIPT_FILENAME
            if not script_file.exists():
                continue
            source = script_file.read_text(encoding="utf-8")
            parts.append(
                f"// === userscript: {entry.name} ===\n"
                f"try {{\n{source}\n}} catch (e) {{\n"
                f"  console.error('[odda] userscript {entry.name} failed:', e);\n"
                f"}}\n"
            )
    (ext_dir / "content.js").write_text("\n".join(parts), encoding="utf-8")
    return ext_dir
