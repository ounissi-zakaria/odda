"""odda's config home: the root of machine-global state.

One environment override redirects everything rooted here — the Base
Chrome profile (``chrome-profile``) and the global rule-slot dir
(``rules``) — so tests and alternate setups never touch the real
``~/.config/odda``. Paths are resolved per call, never cached: a value
computed at import time would freeze whatever override was in effect
when an unrelated module first imported this one.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The environment variable that redirects the config home (an
#: absolute or ``~``-relative directory path).
CONFIG_HOME_ENV = "ODDA_CONFIG_HOME"

#: The Base Chrome profile's directory name under the config home.
BASE_PROFILE_DIR_NAME = "chrome-profile"

#: The global rule-slot directory's name under the config home.
RULES_DIR_NAME = "rules"


def config_home() -> Path:
    """Return odda's config home (remembered, not created here).

    ``~/.config/odda`` unless :data:`CONFIG_HOME_ENV` redirects it.
    Machine-global state only — per-project state lives in the data
    dir, never here.
    """
    override = os.environ.get(CONFIG_HOME_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "odda"


def base_profile_dir() -> Path:
    """Return the Base Chrome profile dir under the config home."""
    return config_home() / BASE_PROFILE_DIR_NAME


def global_rules_dir() -> Path:
    """Return the global rule-slot dir under the config home."""
    return config_home() / RULES_DIR_NAME
