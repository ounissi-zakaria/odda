# See ADR-0038.
from __future__ import annotations

from importlib.resources import files

from odda.scanner.userrules import RULE_FILE_SKELETON

_SKELETON_PLACEHOLDER = "{{RULE_FILE_SKELETON}}\n"


def load_rule_guide() -> str:
    text = files("odda.scanner").joinpath("rule-guide.md").read_text(encoding="utf-8")
    return text.replace(_SKELETON_PLACEHOLDER, RULE_FILE_SKELETON)
