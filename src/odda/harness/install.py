"""Install odda harness plugins and the shared skill into user config dirs.

Supports three harnesses: ``opencode``, ``pi``, ``omp``. Each installs a
plugin/extension into the harness's first-party extension dir and the shared
skill into the harness's first-party skill dir. The skill source is shared
across all harnesses (``odda.harness.skill``); only the install target
differs.
"""

from __future__ import annotations

import shutil
from enum import StrEnum
from importlib import resources
from pathlib import Path


class Harness(StrEnum):
    """Supported harness targets for ``odda install``."""

    opencode = "opencode"
    pi = "pi"
    omp = "omp"


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
