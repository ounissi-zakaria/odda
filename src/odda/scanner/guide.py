"""The rule-authoring guide loader — the one docs resource (ADR-0038).

The ``odda://scanner/rule-guide`` MCP resource serves this text: the
complete authoring surface for scanner rules. It exists because no
tool surface can carry it — the ``rule_install`` description is a
one-line contract, and load-failure rejections can only show the file
shape — while an author cannot write dataset-style rules without the
FlowView API and worked examples. The prose lives in
``rule-guide.md`` next to this module, shipped as package data — the
same storage the removed ``odda://docs/*`` resources used (b3c3b9d).
The rule-file skeleton section is spliced in at serve time from
:data:`~odda.scanner.userrules.RULE_FILE_SKELETON`, the same constant
the rejections teach, so the guide and the errors can never drift
(single source; ticket 18).
"""

from __future__ import annotations

from importlib.resources import files

from odda.scanner.userrules import RULE_FILE_SKELETON

#: Marks where the rule-file skeleton goes inside ``rule-guide.md``.
_SKELETON_PLACEHOLDER = "{{RULE_FILE_SKELETON}}\n"


def load_rule_guide() -> str:
    """Return the ``odda://scanner/rule-guide`` resource payload.

    Reads the shipped markdown and binds :data:`RULE_FILE_SKELETON`
    into its placeholder — the guide's rule-file section is the same
    constant every load-failure rejection appends.
    """
    text = files("odda.scanner").joinpath("rule-guide.md").read_text(encoding="utf-8")
    return text.replace(_SKELETON_PLACEHOLDER, RULE_FILE_SKELETON)
