from __future__ import annotations

import ipaddress
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from odda.paths import config_home, project_data_dir

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

CONFIG_FILE_NAME = "config.toml"


@dataclass(frozen=True, slots=True)
class OddaConfig:
    max_browsers: int = 3
    stale_browser_timeout: int = 0
    vnc_bind: str = "127.0.0.1"


class ConfigError(Exception):
    pass


def _int_setting(key: str, value: Any, source: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(
            f"{source}: {key!r} must be a non-negative integer, got {value!r}"
        )
    return value


def _ip_setting(key: str, value: Any, source: str) -> str:
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


_VALIDATORS: Mapping[str, Callable[[str, Any, str], Any]] = {
    "max_browsers": _int_setting,
    "stale_browser_timeout": _int_setting,
    "vnc_bind": _ip_setting,
}


def _setting(key: str, value: Any, source: str) -> Any:
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
    values: dict[str, Any] = {}
    if global_path is None:
        global_path = config_home() / CONFIG_FILE_NAME
    if project_path is None:
        project_path = project_data_dir() / CONFIG_FILE_NAME
    for path in (global_path, project_path):
        for key, value in _read_toml(path).items():
            values[key] = _setting(key, value, str(path))
    for key, value in (cli_overrides or {}).items():
        if value is None:
            continue
        source = f"command line flag --{key.replace('_', '-')}"
        values[key] = _setting(key, value, source)
    return OddaConfig(**values)


def _read_toml(path: Path) -> dict[str, Any]:
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
