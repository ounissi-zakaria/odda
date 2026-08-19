"""Install odda harness plugins and the shared skill into user config dirs.

Supports four harnesses: ``opencode``, ``pi``, ``omp``, ``claude``. Each
installs a plugin/extension into the harness's first-party extension dir and
the shared skill into the harness's first-party skill dir. The skill source is
shared across all harnesses (``odda.harness.skill``); only the install target
differs. For Claude Code the "plugin" is a self-contained first-class plugin
bundle written to ``~/.claude/skills/odda/`` (a ``.claude-plugin/plugin.json``
manifest plus a ``SessionStart`` hook and the shared skill); Claude Code
auto-loads it as a skills-directory plugin, so the user's
``~/.claude/settings.json`` is never touched.
"""

from __future__ import annotations

import json
import shutil
from enum import StrEnum
from importlib import resources
from pathlib import Path

from odda import __version__


class Harness(StrEnum):
    """Supported harness targets for ``odda install``."""

    opencode = "opencode"
    pi = "pi"
    omp = "omp"
    claude = "claude"


def install_harness(harness: Harness) -> tuple[Path, Path]:
    """Install the plugin and skill for the given harness.

    Args:
        harness: Which harness to install for.

    Returns:
        Tuple of (plugin destination path, skill destination path).

    Raises:
        RuntimeError: If the source assets cannot be found or copied.
    """
    if harness is Harness.opencode:
        return _install_opencode()
    if harness is Harness.claude:
        return _install_claude()
    return _install_pi_family(harness)


def _install_opencode() -> tuple[Path, Path]:
    """Copy the OpenCode plugin and skill into ``~/.config/opencode/``."""
    config_dir = Path.home() / ".config" / "opencode"
    plugins_dir = config_dir / "plugins"
    skills_dir = config_dir / "skills" / "odda"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)

    try:
        with resources.as_file(resources.files("odda.harness.opencode")) as asset_dir:
            plugin_src = asset_dir / "plugin.js"
            if not plugin_src.exists():
                msg = "OpenCode plugin not found in odda package"
                raise RuntimeError(msg)
            plugin_dst = plugins_dir / "odda.js"
            shutil.copy2(plugin_src, plugin_dst)
    except (OSError, ValueError) as exc:
        msg = f"Failed to install OpenCode plugin: {exc}"
        raise RuntimeError(msg) from exc

    skill_dst = _install_skill(skills_dir)
    return plugin_dst, skill_dst


def _install_pi_family(harness: Harness) -> tuple[Path, Path]:
    """Copy the shared pi/omp extension and skill into the harness config dir."""
    config_root = ".pi" if harness is Harness.pi else ".omp"
    agent_dir = Path.home() / config_root / "agent"
    extensions_dir = agent_dir / "extensions"
    skills_dir = agent_dir / "skills" / "odda"
    extensions_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)

    plugin_name = "plugin-pi.ts"
    try:
        with resources.as_file(resources.files("odda.harness.pi")) as asset_dir:
            plugin_src = asset_dir / plugin_name
            if not plugin_src.exists():
                msg = f"{harness.value} plugin not found in odda package"
                raise RuntimeError(msg)
            plugin_dst = extensions_dir / "odda.ts"
            shutil.copy2(plugin_src, plugin_dst)
    except (OSError, ValueError) as exc:
        msg = f"Failed to install {harness.value} plugin: {exc}"
        raise RuntimeError(msg) from exc

    skill_dst = _install_skill(skills_dir)
    return plugin_dst, skill_dst


def _install_claude() -> tuple[Path, Path]:
    """Install the odda plugin bundle and skill into ``~/.claude/skills/odda/``.

    Claude Code auto-loads a self-contained plugin from any directory under
    ``~/.claude/skills/`` that holds a ``.claude-plugin/plugin.json`` manifest
    (the skills-directory plugin mechanism — no marketplace needed). odda's
    bundle carries a ``SessionStart`` hook (``hooks/hooks.json`` →
    ``scripts/start.py``) that starts the per-session server and writes the
    ``ODDA_*`` env into ``$CLAUDE_ENV_FILE``, plus the shared skill under
    ``skills/odda/``. The user's ``~/.claude/settings.json`` is never touched.
    """
    claude_dir = Path.home() / ".claude"
    plugin_dir = claude_dir / "skills" / "odda"
    (plugin_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_dir / "hooks").mkdir(parents=True, exist_ok=True)
    (plugin_dir / "scripts").mkdir(parents=True, exist_ok=True)
    skills_dir = plugin_dir / "skills" / "odda"
    skills_dir.mkdir(parents=True, exist_ok=True)

    _write_plugin_manifest(plugin_dir / ".claude-plugin" / "plugin.json")
    _write_plugin_hooks(plugin_dir / "hooks" / "hooks.json")

    try:
        with resources.as_file(resources.files("odda.harness.claude")) as asset_dir:
            hook_src = asset_dir / "start.py"
            if not hook_src.exists():
                msg = "Claude Code hook script not found in odda package"
                raise RuntimeError(msg)
            hook_dst = plugin_dir / "scripts" / "start.py"
            shutil.copy2(hook_src, hook_dst)
            hook_dst.chmod(0o755)
    except (OSError, ValueError) as exc:
        msg = f"Failed to install Claude Code hook: {exc}"
        raise RuntimeError(msg) from exc

    skill_dst = _install_skill(skills_dir)
    return plugin_dir, skill_dst


def _write_plugin_manifest(manifest_path: Path) -> None:
    """Write the plugin manifest (``plugin.json``) for the odda bundle.

    Generated inline so the version tracks ``odda.__version__`` without a
    static template file to keep in sync.
    """
    manifest = {
        "name": "odda",
        "version": __version__,
        "description": (
            "Browser automation, HTTP traffic capture, dynamic analysis, "
            "and raw request crafting via the odda CLI. Starts a per-session "
            "odda server and teaches the agent the odda CLI surface."
        ),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _write_plugin_hooks(hooks_path: Path) -> None:
    """Write the plugin's ``SessionStart`` hook config.

    Uses the ``${CLAUDE_PLUGIN_ROOT}`` placeholder (resolved by Claude Code to
    the bundle dir) and the exec form (``command`` + ``args``) so the script
    path is passed as a single argument with no shell-quoting risk. The hook is
    Python (``python3 …/start.py``); the hook's runtime is the same ``python3``
    odda already requires, and a Python hook can probe a Unix socket natively.
    """
    hooks = {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3",
                            "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/start.py"],
                        }
                    ]
                }
            ]
        }
    }
    hooks_path.write_text(json.dumps(hooks, indent=2) + "\n", encoding="utf-8")


def _install_skill(skills_dir: Path) -> Path:
    """Copy the shared skill tree into ``skills_dir``.

    Mirrors ``SKILL.md`` plus disclosed reference ``.md`` files and the
    ``recipes/`` subfolder, removing stale entries at the destination so
    deletions propagate.
    """
    try:
        with resources.as_file(resources.files("odda.harness.skill")) as skill_dir:
            skill_src = skill_dir / "SKILL.md"
            if not skill_src.exists():
                msg = "Skill SKILL.md not found in odda package"
                raise RuntimeError(msg)
            skill_dst = skills_dir / "SKILL.md"
            shutil.copy2(skill_src, skill_dst)
            _sync_skill_assets(skill_dir, skills_dir)
    except (OSError, ValueError) as exc:
        msg = f"Failed to install skill assets: {exc}"
        raise RuntimeError(msg) from exc

    return skill_dst


def _sync_skill_assets(asset_dir: Path, skills_dir: Path) -> None:
    """Copy disclosed reference ``.md`` files and the ``recipes/`` tree."""
    _mirror_md_files(asset_dir, skills_dir, exclude={"SKILL.md"})
    src_recipes = asset_dir / "recipes"
    dst_recipes = skills_dir / "recipes"
    if not src_recipes.is_dir():
        if dst_recipes.is_dir():
            shutil.rmtree(dst_recipes)
        return
    dst_recipes.mkdir(parents=True, exist_ok=True)
    _mirror_md_files(src_recipes, dst_recipes)


def _mirror_md_files(
    src: Path, dst: Path, *, exclude: frozenset[str] = frozenset()
) -> None:
    """Copy ``*.md`` files from ``src`` to ``dst``, removing stale ones at ``dst``."""
    names = {p.name for p in src.glob("*.md") if p.name not in exclude}
    for name in names:
        shutil.copy2(src / name, dst / name)
    for existing in dst.glob("*.md"):
        if existing.name not in exclude and existing.name not in names:
            existing.unlink()
