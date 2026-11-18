from __future__ import annotations

import re
from functools import cache
from typing import Any

from odda.scanner.plumbing import GIL_CHUNK, chunked, sre_constants, sre_parse

_GATE_MIN_LITERAL = 3


def _fold_chunk(text: str) -> str:
    return text.casefold().replace("\u0307", "").replace("\u0131", "i")


def _gate_fold(text: str) -> str:
    if len(text) <= GIL_CHUNK:
        return _fold_chunk(text)
    return "".join(_fold_chunk(part) for part in chunked(text))


def _ci_gates_pass(gates: tuple[frozenset[str], ...], text: str) -> bool:
    span = max((len(lit) for s in gates for lit in s), default=0)
    if span >= GIL_CHUNK:
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
    if len(text) <= GIL_CHUNK or len(literal) >= GIL_CHUNK:
        return literal in text
    for window in chunked(text, step=GIL_CHUNK - len(literal) + 1):
        if literal in window:
            return True
    return False


@cache
def _literal_gates(source: str, flags: int) -> tuple[frozenset[str], ...] | None:
    if sre_parse is None:
        return None
    try:
        tree = sre_parse(source, flags)
    except Exception:
        return None
    sets = _concat_gates(tree)
    if flags & re.IGNORECASE:
        sets = [
            frozenset(_gate_fold(lit) for lit in s)
            for s in sets
            if all(lit.isascii() for lit in s)
        ]
    return tuple(sets) or None


def _branch_gate(disjuncts: Any) -> frozenset[str] | None:
    alts: set[str] = set()
    for disjunct in disjuncts:
        sets = _concat_gates(disjunct)
        if not sets:
            return None
        for gate_set in sets:
            alts.update(gate_set)
    return frozenset(alts) or None


def _dispatch_gates(op: Any, av: Any, out: list[frozenset[str]]) -> None:
    if op is sre_constants.SUBPATTERN:
        if not av[1] & re.IGNORECASE:
            out.extend(_concat_gates(av[-1]))
    elif op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT):
        if av[0] >= 1:
            out.extend(_concat_gates(av[-1]))
    elif op is sre_constants.BRANCH:
        branch = _branch_gate(av[1])
        if branch is not None:
            out.append(branch)


def _concat_gates(node: Any) -> list[frozenset[str]]:
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
    gates = _literal_gates(pattern.pattern, pattern.flags)
    if gates is None:
        return True
    if pattern.flags & re.IGNORECASE:
        if len(text) > GIL_CHUNK:
            return _ci_gates_pass(gates, text)
        text = _gate_fold(text)
    return all(any(_gate_contains(lit, text) for lit in s) for s in gates)
