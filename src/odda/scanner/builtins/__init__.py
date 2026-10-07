"""Builtin rules: one module per family, composed into one list (ADR-0035).

Adding a family means adding its module and extending
:data:`BUILTIN_RULES` — no registration machinery, no discovery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from odda.scanner.builtins import disclosure

if TYPE_CHECKING:
    from odda.scanner.model import Rule

BUILTIN_RULES: tuple[Rule, ...] = (*disclosure.RULES,)
