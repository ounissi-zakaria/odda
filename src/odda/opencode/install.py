"""Install OpenCode plugin and skill files into the user's config directory."""

from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path


def install_opencode_assets() -> tuple[Path, Path]:
    """Copy the OpenCode plugin and skill into ``~/.config/opencode/``.

    Syncs the full skill directory (SKILL.md plus disclosed reference files and
    the ``recipes/`` subfolder) so progressive-disclosure targets travel with
    the skill. Returns the plugin and skill destination paths.

    Returns:
        Tuple of (plugin destination path, skill destination path).

    Raises:
        RuntimeError: If the source assets cannot be found.
    """
    config_dir = Path.home() / ".config" / "opencode"
    plugins_dir = config_dir / "plugins"
    skills_dir = config_dir / "skills" / "odda"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)

    try:
        with resources.as_file(resources.files("odda.opencode")) as asset_dir:
            plugin_src = asset_dir / "plugin.js"
            skill_src = asset_dir / "SKILL.md"
            if not plugin_src.exists() or not skill_src.exists():
                msg = "OpenCode assets not found in odda package"
                raise RuntimeError(msg)
            plugin_dst = plugins_dir / "odda.js"
            skill_dst = skills_dir / "SKILL.md"
            shutil.copy2(plugin_src, plugin_dst)
            shutil.copy2(skill_src, skill_dst)
            _sync_skill_assets(asset_dir, skills_dir)
    except (OSError, ValueError) as exc:
        msg = f"Failed to install OpenCode assets: {exc}"
        raise RuntimeError(msg) from exc

    return plugin_dst, skill_dst


def _sync_skill_assets(asset_dir: Path, skills_dir: Path) -> None:
    """Copy disclosed reference ``.md`` files and the ``recipes/`` tree.

    Removes stale reference files and recipe files at the destination that no
    longer exist at the source, so deletions propagate.
    """
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
