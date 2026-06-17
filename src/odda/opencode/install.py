"""Install OpenCode plugin and skill files into the user's config directory."""

from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path


def install_opencode_assets() -> tuple[Path, Path]:
    """Copy the OpenCode plugin and skill into ``~/.config/opencode/``.

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
    except (OSError, ValueError) as exc:
        msg = f"Failed to install OpenCode assets: {exc}"
        raise RuntimeError(msg) from exc

    return plugin_dst, skill_dst
