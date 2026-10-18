"""Passive scanner package; ``odda.scanner`` is the rule-author surface.

User rule files import the names below — this is the stable contract
ADR-0035 pins, kept additive across odda upgrades. No rules ship with
odda (ADR-0036): every rule lives in a slot under the global rules
dir, and the engine is internal machinery (imported by the MCP
lifespan), not part of the author surface.
"""

from __future__ import annotations

from odda.scanner.model import Correlator, Evidence, Finding, FlowView, Rule, Severity

__all__ = [
    "Correlator",
    "Evidence",
    "Finding",
    "FlowView",
    "Rule",
    "Severity",
]
