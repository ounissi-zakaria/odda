"""Necessary-literal gates: the pre-regex screen every stdlib scan runs.

A body-regex scan is one C call that holds the GIL for its whole
duration, so with scans off the event-loop thread (engine.run) a
multi-MB body still turns each unmatched pattern into a
tens-to-hundreds-of-ms loop stall. The gates below restore
interactivity: per pattern, a tuple of literal sets such that any
match must put at least one literal of each set into the scanned
text — spine literals as singleton sets, one any-of set per
alternation. A failing ``in`` check (fastsearch speed) replaces a
linear regex scan for free.

Soundness is conservative by construction: case-insensitive
patterns (global or scoped), character classes, zero-width and
optional constructs contribute nothing, so a pattern without a
usable literal simply has no gate and scans ungated. The fuzz test
in tests/e2e/test_30_scanner.py pins the property that matters: a
real match is never gated out.

This module is the gate machinery's one home (moved out of the
rule-author contract module, model.py): the engine's stdlib residual
scans and rule slots' own residuals (the retire.js adapter) both go
through the public :func:`passes_literal_gates` — a slot source
outside the package must not depend on a private engine symbol.
"""

from __future__ import annotations

import re
from functools import cache
from typing import Any

from odda.scanner.plumbing import GIL_CHUNK, chunked, sre_constants, sre_parse

_GATE_MIN_LITERAL = 3


def _fold_chunk(text: str) -> str:
    """One fold slice.

    Case folding is per character, so slicing is exact: folding a
    concatenation concatenates the folds.
    """
    return text.casefold().replace("\u0307", "").replace("\u0131", "i")


def _gate_fold(text: str) -> str:
    """The fold the case-insensitive gates check against, chunked.

    Large texts fold in GIL-yielding chunks (``chunked``'s contract).
    Full case folding normalized to Python's per-character (?i)
    classes: folding expands U+0130 to ``i`` + U+0307 (dot stripped)
    and leaves U+0131 distinct, while Python's (?i) matches both
    against ``i`` — normalizing each to ``i`` keeps a folded literal a
    substring of the folded text for every real match (the sweep in
    tests/e2e/test_30_scanner.py pins the per-character edge).
    """
    if len(text) <= GIL_CHUNK:
        return _fold_chunk(text)
    return "".join(_fold_chunk(part) for part in chunked(text))


def _ci_gates_pass(gates: tuple[frozenset[str], ...], text: str) -> bool:
    """Case-insensitive gate check without folding the whole text.

    Folds chunk by chunk (exact: folding is per character) and checks
    the still-unmet sets against ``carry + folded chunk``, so no whole-
    body folded copy is ever built — the one remaining 10 MB-scale C
    call the join would cost is exactly the loop stall the chunking
    exists to avoid. The carry keeps the last ``span`` folded
    characters, so a literal straddling a chunk boundary is never
    missed.
    """
    span = max((len(lit) for s in gates for lit in s), default=0)
    if span >= GIL_CHUNK:  # pathological literal: windows cannot cover it
        folded = _gate_fold(text)
        return all(any(lit in folded for lit in s) for s in gates)
    pending = list(gates)
    carry = ""
    for part in chunked(text):
        folded = carry + _fold_chunk(part)
        pending = [s for s in pending if not any(lit in folded for lit in s)]
        if not pending:
            return True
        carry = folded[-span:] if span else ""
    return not pending


def _gate_contains(literal: str, text: str) -> bool:
    """Chunked substring containment (the gate scans' one linear op).

    Windows overlap by the literal's length (``chunked``'s ``step``),
    so an occurrence spanning a boundary is never missed; each slice
    is one short C call with a GIL drop after it, instead of one call
    over the whole body.
    """
    if len(text) <= GIL_CHUNK or len(literal) >= GIL_CHUNK:
        return literal in text
    for window in chunked(text, step=GIL_CHUNK - len(literal) + 1):
        if literal in window:
            return True
    return False


@cache
def _literal_gates(source: str, flags: int) -> tuple[frozenset[str], ...] | None:
    """Necessary-literal sets for one pattern, or ``None`` (no gate).

    Args:
        source: The pattern's source string.
        flags: The pattern's compiled flags (globally case-insensitive
            patterns gate on casefolded literals).

    Returns:
        A tuple of literal sets — every match of the pattern puts at
        least one literal of each set into the scanned text (both
        sides casefolded, for globally case-insensitive patterns) —
        or ``None`` when no sound gate is extractable.
    """
    if sre_parse is None:
        return None
    try:
        tree = sre_parse(source, flags)
    except Exception:
        return None
    sets = _concat_gates(tree)
    if flags & re.IGNORECASE:
        # Full case folding is sound for Python's per-character
        # simple-folded (?i) matching: whatever the pattern matches,
        # the folded literal rides in the folded text. Sets holding a
        # non-ASCII literal drop out whole — an any-of set minus a
        # member could gate out the branch that matched — which only
        # weakens the gate. Scoped (?i:...) groups still never
        # contribute literals (see _dispatch_gates).
        sets = [
            frozenset(_gate_fold(lit) for lit in s)
            for s in sets
            if all(lit.isascii() for lit in s)
        ]
    return tuple(sets) or None


def _branch_gate(disjuncts: Any) -> frozenset[str] | None:
    """One any-of set for an alternation.

    A match takes some branch, and that branch's literals then appear
    in the scanned text, so the sound any-of set is the union of every
    disjunct's literal sets. Two shapes forfeit the gate instead of
    weakening it unsoundly: a disjunct with no extractable literal
    (``go`` next to ``prometheus`` — the match may have taken exactly
    that branch), and a set reduced to one member (``{200, 201}`` kept
    as ``{200}`` — the match may have shown the dropped member).
    """
    alts: set[str] = set()
    for disjunct in disjuncts:
        sets = _concat_gates(disjunct)
        if not sets:
            return None
        for gate_set in sets:
            alts.update(gate_set)
    return frozenset(alts) or None


def _dispatch_gates(op: Any, av: Any, out: list[frozenset[str]]) -> None:
    """Add the gate sets of one non-literal parse node to ``out``."""
    if op is sre_constants.SUBPATTERN:
        # av = (group, add_flags, del_flags, sub). Scoped inline flags
        # ((?i:...)) make the group's literals case insensitive — not
        # sound as case-sensitive gates, so a flagged group stays out.
        if not av[1] & re.IGNORECASE:
            out.extend(_concat_gates(av[-1]))
    elif op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT):
        if av[0] >= 1:  # required at least once: in every match
            out.extend(_concat_gates(av[-1]))
    elif op is sre_constants.BRANCH:
        branch = _branch_gate(av[1])
        if branch is not None:
            out.append(branch)
    # classes, anchors, lookarounds, backreferences, and conditionals
    # contribute nothing; skipping them is always sound.


def _concat_gates(node: Any) -> list[frozenset[str]]:
    """Literal gate sets of one parsed concatenation (see _literal_gates)."""
    out: list[frozenset[str]] = []
    run = ""
    for op, av in node:
        if op is sre_constants.LITERAL:
            run += chr(av)
            continue
        if len(run) >= _GATE_MIN_LITERAL:
            out.append(frozenset((run,)))
        run = ""
        _dispatch_gates(op, av, out)
    if len(run) >= _GATE_MIN_LITERAL:
        out.append(frozenset((run,)))
    return out


def passes_literal_gates(pattern: re.Pattern[str], text: str) -> bool:
    """Whether ``pattern`` should run on ``text``: no gate, or all pass.

    A globally case-insensitive pattern checks its folded literals
    against the casefolded text (one fold per call — the scanned
    texts are tiny or already off the event loop's thread). This is
    the one public gate seam: the engine's stdlib residual scans and
    rule slots' own residuals call it before any linear regex pass.
    """
    gates = _literal_gates(pattern.pattern, pattern.flags)
    if gates is None:
        return True
    if pattern.flags & re.IGNORECASE:
        if len(text) > GIL_CHUNK:
            return _ci_gates_pass(gates, text)
        text = _gate_fold(text)
    return all(any(_gate_contains(lit, text) for lit in s) for s in gates)
