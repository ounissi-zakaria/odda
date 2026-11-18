"""odda's session configuration: three levels, one precedence.

Machine-operator settings arrive from three places, later wins:

1. the global ``config.toml`` under the odda config home
   (``~/.config/odda`` unless ``ODDA_CONFIG_HOME`` redirects it),
2. the project ``config.toml`` in the data dir (``.odda/`` next to
   ``flows.jsonl``),
3. CLI flags on ``odda mcp``.

The file format is TOML, read once when the MCP session boots — no
hot reload: a session runs on its boot-time values. Files that don't
exist contribute nothing; files that exist but carry an unknown key or
invalid value fail the boot with the file and key named (a typo'd
lever that silently no-ops is the worst failure mode).
"""

from __future__ import annotations

import ipaddress
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from odda.paths import config_home

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

#: The config file's name at both levels (config home and data dir).
CONFIG_FILE_NAME = "config.toml"


@dataclass(frozen=True, slots=True)
class OddaConfig:
    """One session's resolved settings.

    ``max_browsers``: concurrently open browsers this session allows;
    ``0`` means unlimited. ``stale_browser_timeout``: seconds a browser
    may go without a tool call targeting it before it auto-closes;
    ``0`` disables the sweep. ``vnc_bind``: the IP address the
    per-browser noVNC endpoints bind (loopback by default; raw VNC
    ports stay loopback-only).
    """

    max_browsers: int = 3
    stale_browser_timeout: int = 0
    vnc_bind: str = "127.0.0.1"


class ConfigError(Exception):
    """A config file or CLI override carries an unknown key or invalid value."""


def _int_setting(key: str, value: Any, source: str) -> int:
    """Validate one integer lever (bool is not an int here)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(
            f"{source}: {key!r} must be a non-negative integer, got {value!r}"
        )
    return value


def _ip_setting(key: str, value: Any, source: str) -> str:
    """Validate one IP-literal lever (hostnames are rejected)."""
    bad = (
        f"{source}: {key!r} must be an IP address literal "
        f'(e.g. "127.0.0.1"), got {value!r}'
    )
    if not isinstance(value, str):
        raise ConfigError(bad)
    try:
        ipaddress.ip_address(value)
    except ValueError:
        raise ConfigError(bad) from None
    return value


#: One validator per config key; the key set is the contract.
_VALIDATORS: Mapping[str, Callable[[str, Any, str], Any]] = {
    "max_browsers": _int_setting,
    "stale_browser_timeout": _int_setting,
    "vnc_bind": _ip_setting,
}


def _setting(key: str, value: Any, source: str) -> Any:
    """Validate one setting under ``source``'s name (file path or flag)."""
    validator = _VALIDATORS.get(key)
    if validator is None:
        valid = ", ".join(sorted(_VALIDATORS))
        raise ConfigError(f"{source}: unknown config key {key!r}; valid keys: {valid}")
    return validator(key, value, source)


def load_config(
    cli_overrides: Mapping[str, Any] | None = None,
    *,
    global_path: Path | None = None,
    project_path: Path | None = None,
) -> OddaConfig:
    """Merge the three levels into one :class:`OddaConfig`.

    ``cli_overrides`` maps config keys to flag values (``None``-valued
    or absent keys contribute nothing). ``global_path`` defaults to the
    config home's ``config.toml``; ``project_path`` defaults to
    ``.odda/config.toml`` under the current working directory — the
    same resolution as the MCP lifespan's data dir. Raises
    :class:`ConfigError` naming the offending file (or CLI flag), key,
    and value.
    """
    values: dict[str, Any] = {}
    if global_path is None:
        global_path = config_home() / CONFIG_FILE_NAME
    if project_path is None:
        project_path = Path(".odda").resolve() / CONFIG_FILE_NAME
    for path in (global_path, project_path):
        for key, value in _read_toml(path).items():
            values[key] = _setting(key, value, str(path))
    for key, value in (cli_overrides or {}).items():
        source = f"command line flag --{key.replace('_', '-')}"
        values[key] = _setting(key, value, source)
    return OddaConfig(**values)


def _read_toml(path: Path) -> dict[str, Any]:
    """Read one config file's settings (empty when the file is absent)."""
    try:
        with path.open("rb") as f:
            settings = tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML ({exc})") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read config ({exc})") from exc
    if not isinstance(settings, dict):
        raise ConfigError(f"{path}: top level must be a table of settings")
    return settings
