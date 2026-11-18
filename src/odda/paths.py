from __future__ import annotations

import os
from pathlib import Path

CONFIG_HOME_ENV = "ODDA_CONFIG_HOME"

BASE_PROFILE_DIR_NAME = "chrome-profile"

RULES_DIR_NAME = "rules"


def config_home() -> Path:
    override = os.environ.get(CONFIG_HOME_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "odda"


def base_profile_dir() -> Path:
    return config_home() / BASE_PROFILE_DIR_NAME


def global_rules_dir() -> Path:
    return config_home() / RULES_DIR_NAME


DATA_DIR_NAME = ".odda"


def project_data_dir() -> Path:
    # See ADR-0017.
    return Path(DATA_DIR_NAME).resolve()
