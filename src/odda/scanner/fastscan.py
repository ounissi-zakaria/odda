r"""Multi-pattern find-all seam over the Vectorscan-backed engine.

One :class:`FastScan` holds a compiled multi-pattern database built from
many :class:`re.Pattern` objects and answers, per input pattern, the match
spans over any decoded body text — character offsets, leftmost-first,
non-overlapping, byte-identical to ``re.Pattern.finditer`` over the exact
text the Scanner scans (``FlowView.body``: leniently UTF-8-decoded str).

Engine design
=============

The engine (``hyperscan``, Vectorscan-backed; a hard dependency — there is
no fallback engine) reports, per end offset, the match with the leftmost
start — not the full match relation Python iterates. So each pattern is
serialized in *reversed* form (anchors swapped, quantifier order flipped)
and scanned against the reversed body: one reversed-scan pass then yields
every true match start. The spans themselves are selected by anchored
stdlib matches at those candidate starts, which makes the output equal to
``finditer`` by construction — candidates are a superset of true starts,
and the anchored match reproduces Python's leftmost-first preference
(greedy/lazy/alternation order) exactly. The GIL is released around the
engine's scan calls; the anchored Python work only ever touches actual
matches.

Divergence guards
=================

The engine's Unicode tables are not Python's:

- ``\w``/``\d``/``\s`` classes (and negations) diverge from Python's on a
  few thousand non-ASCII code points. Such patterns compile without UCP
  (ASCII flavor) and are routed through the stdlib path whenever the body
  is not pure ASCII; on ASCII bodies both engines agree exactly.
- Case-insensitive folding diverges only on the Turkish dotted I
  (U+0130/U+0131). Caseless patterns compiled with UCP take the stdlib
  path when the body contains either.

Prefilters
==========

Patterns the engine refuses — backreferences, lookarounds, ``\b``/``\B``,
possessive/atomic repeats, and patterns over the engine's size budget —
compile into the same pass as *approximate prefilters* (the engine's
prefilter mode: they may over-report, never under-report).
:meth:`FastScan.prefiltered` reports which input indices run approximated;
their spans are approximation hits (end offsets are real, starts are
reported as 0). Patterns the engine can neither scan nor approximate —
above all patterns that can match the empty string, where ``finditer``
emits zero-width spans no engine run could reproduce — are refused at
build with a ``ValueError`` naming the pattern; callers run those on the
stdlib path outright.
"""

from __future__ import annotations

import re
import time
from re import _constants as _sre
from re._parser import parse as _sre_parse
from typing import TYPE_CHECKING

import hyperscan

if TYPE_CHECKING:
    from collections.abc import Sequence

# Anchor translation for the reversed expression. ``$`` becomes ``^`` (and
# forces MULTILINE, whose "after any newline" is a superset of the reversed
# image of Python's trailing-newline ``$``); ``^`` becomes ``$``; the buffer
# anchors swap; ``\b``/``\B`` have no engine equivalent under UCP.
_AT_REVERSED = {
    _sre.AT_BEGINNING: "$",
    _sre.AT_BEGINNING_STRING: r"\z",
    _sre.AT_END: "^",
    _sre.AT_END_STRING: r"\A",
    _sre.AT_BOUNDARY: None,
    _sre.AT_NON_BOUNDARY: None,
}
_CATEGORY = {
    _sre.CATEGORY_DIGIT: r"\d",
    _sre.CATEGORY_NOT_DIGIT: r"\D",
    _sre.CATEGORY_SPACE: r"\s",
    _sre.CATEGORY_NOT_SPACE: r"\S",
    _sre.CATEGORY_WORD: r"\w",
    _sre.CATEGORY_NOT_WORD: r"\W",
}
_UNI_CLASS_CATEGORIES = frozenset(_CATEGORY)
# Characters where the engine's simple case folding differs from Python's
# full folding (U+0130 LATIN CAPITAL LETTER I WITH DOT ABOVE, U+0131 DOTLESS
# SMALL I) — verified by sweep over the BMP and the astral cased scripts.
_FOLD_DIVERGENT = "\u0130\u0131"
# PCRE metacharacters that must be escaped when re-emitting literals.
_META_OUTSIDE = set("\\^$.|?*+()[]{}")
_META_IN_CLASS = set("\\]^-")
_ASCII_CTRL = 0x20
_ASCII_DEL = 0x7F
_UTF8_CONT_LO = 0x80
_UTF8_CONT_HI = 0xBF

_RE_LEADING_GLOBAL_FLAGS = re.compile(r"^\(\?[aiLmsux]+\)")


class _UnsupportedError(Exception):
    """The pattern uses a construct the exact path cannot serialize."""


def _escape(ch: str, *, in_class: bool = False) -> str:
    """Emit one literal character as engine-safe source."""
    code = ord(ch)
    if code < _ASCII_CTRL or code == _ASCII_DEL:
        return f"\\x{code:02x}"
    if code > _ASCII_DEL:
        return ch
    meta = _META_IN_CLASS if in_class else _META_OUTSIDE
    return "\\" + ch if ch in meta else ch


def _class_source(items: Sequence) -> str:
    parts: list[str] = []
    negate = ""
    for op, av in items:
        if op is _sre.NEGATE:
            negate = "^"
        elif op is _sre.LITERAL:
            parts.append(_escape(chr(av), in_class=True))
        elif op is _sre.RANGE:
            parts.append(
                _escape(chr(av[0]), in_class=True)
                + "-"
                + _escape(chr(av[1]), in_class=True)
            )
        elif op is _sre.CATEGORY:
            parts.append(_CATEGORY[av])
        else:
            raise _UnsupportedError(f"character-class item {op}")
    return f"[{negate}{''.join(parts)}]"


def _min_length(tree) -> int:
    """Minimum match length in characters; 0 means it can match empty."""
    total = 0
    for op, av in tree:
        if op in (_sre.LITERAL, _sre.IN, _sre.ANY, _sre.NOT_LITERAL):
            total += 1
        elif op is _sre.SUBPATTERN:
            total += _min_length(av[3])
        elif op in (_sre.MAX_REPEAT, _sre.MIN_REPEAT):
            total += av[0] * _min_length(av[2])
        elif op is _sre.BRANCH:
            total += min(_min_length(branch) for branch in av[1])
        elif op is _sre.AT:
            continue
        else:
            # Lookarounds, backreferences, conditionals, possessive and
            # atomic repeats: the exact path does not serialize them.
            raise _UnsupportedError(f"construct {op}")
    return total


def _reversed_repeat(av) -> str:
    """Reversed source for a repeat node.

    Laziness does not change the set of match starts, so both quantifier
    flavors emit greedy.
    """
    mn, mx, sub = av
    body, atom = _reversed_single(sub)
    if not atom:
        body = f"(?:{body})"
    if mx == _sre.MAXREPEAT:
        if mn == 0:
            return body + "*"
        if mn == 1:
            return body + "+"
        return f"{body}{{{mn},}}"
    if mn == mx:
        return f"{body}{{{mn}}}"
    if (mn, mx) == (0, 1):
        return body + "?"
    return f"{body}{{{mn},{mx}}}"


def _reversed_item(node) -> tuple[str, bool]:
    """Reversed source for one sequence item.

    Returns the source and whether it is a single atom (safe to quantify
    without grouping).
    """
    op, av = node
    if op is _sre.LITERAL:
        return _escape(chr(av)), True
    if op is _sre.IN:
        return _class_source(av), True
    if op is _sre.ANY:
        return ".", True
    if op is _sre.NOT_LITERAL:
        return f"[^{_escape(chr(av), in_class=True)}]", True
    if op is _sre.AT:
        token = _AT_REVERSED[av]
        if token is None:
            raise _UnsupportedError(f"anchor {av}")
        return token, True
    if op is _sre.SUBPATTERN:
        return f"(?:{_reversed_source(av[3])})", True
    if op is _sre.BRANCH:
        alts = "|".join(_reversed_source(branch) for branch in av[1])
        return f"(?:{alts})", True
    if op in (_sre.MAX_REPEAT, _sre.MIN_REPEAT):
        return _reversed_repeat(av), True
    raise _UnsupportedError(f"construct {op}")


def _reversed_single(tree) -> tuple[str, bool]:
    """Reversed source of a whole subexpression treated as one unit."""
    items = list(tree)
    if len(items) == 1:
        return _reversed_item(items[0])
    return _reversed_source(items), False


def _reversed_source(tree) -> str:
    items = [_reversed_item(node) for node in tree]
    return "".join(source for source, _ in reversed(items))


def _walk_tree(tree, fn) -> None:
    """Visit every node of a parsed tree, parents before children."""
    for node in tree:
        fn(node)
        op, av = node
        if op is _sre.SUBPATTERN:
            _walk_tree(av[3], fn)
        elif op is _sre.BRANCH:
            for branch in av[1]:
                _walk_tree(branch, fn)
        elif op in (_sre.MAX_REPEAT, _sre.MIN_REPEAT):
            _walk_tree(av[2], fn)


def _inline_flags(tree) -> int:
    flags = 0

    def collect(node) -> None:
        nonlocal flags
        op, av = node
        if op is _sre.SUBPATTERN and av[1]:
            flags |= av[1]

    _walk_tree(tree, collect)
    return flags


def _contains_at_end(tree) -> bool:
    found = False

    def check(node) -> None:
        nonlocal found
        op, av = node
        if op is _sre.AT and av is _sre.AT_END:
            found = True

    _walk_tree(tree, check)
    return found


def _uses_unicode_class(tree) -> bool:
    uses = False

    def check(node) -> None:
        nonlocal uses
        op, av = node
        if op is _sre.IN and any(
            item[0] is _sre.CATEGORY and item[1] in _UNI_CLASS_CATEGORIES for item in av
        ):
            uses = True

    _walk_tree(tree, check)
    return uses


def _reverse_expression(pattern: re.Pattern) -> tuple[str, int, bool, bool]:
    """Build the engine expression for one pattern.

    Returns the reversed source, the engine flags, whether the classes were
    compiled ASCII-flavored (valid only for ASCII bodies), and whether
    case folding can diverge on the Turkish dotted I.
    """
    flags = pattern.flags
    tree = _sre_parse(pattern.pattern, flags)
    if _min_length(tree) == 0:
        raise _UnsupportedError("can match the empty string")
    source = _reversed_source(tree)
    inline = _inline_flags(tree)
    hs_flags = hyperscan.HS_FLAG_UTF8 | hyperscan.HS_FLAG_SOM_LEFTMOST
    unicode_classes = _uses_unicode_class(tree) and not flags & re.ASCII
    if not unicode_classes and not flags & re.ASCII:
        hs_flags |= hyperscan.HS_FLAG_UCP
    if flags & re.IGNORECASE or inline & re.IGNORECASE:
        hs_flags |= hyperscan.HS_FLAG_CASELESS
    if flags & re.DOTALL or inline & re.DOTALL:
        hs_flags |= hyperscan.HS_FLAG_DOTALL
    if flags & re.MULTILINE or inline & re.MULTILINE or _contains_at_end(tree):
        hs_flags |= hyperscan.HS_FLAG_MULTILINE
    fold_sensitive = bool(
        hs_flags & hyperscan.HS_FLAG_CASELESS and hs_flags & hyperscan.HS_FLAG_UCP
    )
    return source, hs_flags, unicode_classes, fold_sensitive


def _prefilter_expression(pattern: re.Pattern) -> tuple[str, int]:
    """Build the approximate-prefilter compile for one pattern.

    Returns the original source (global inline flags folded away — the
    engine rejects bare ``(?u)``) and the engine flags.
    """
    flags = pattern.flags
    source = _RE_LEADING_GLOBAL_FLAGS.sub("", pattern.pattern, count=1)
    hs_flags = hyperscan.HS_FLAG_UTF8 | hyperscan.HS_FLAG_PREFILTER
    if not flags & re.ASCII:
        hs_flags |= hyperscan.HS_FLAG_UCP
    if flags & re.IGNORECASE:
        hs_flags |= hyperscan.HS_FLAG_CASELESS
    if flags & re.DOTALL:
        hs_flags |= hyperscan.HS_FLAG_DOTALL
    if flags & re.MULTILINE:
        hs_flags |= hyperscan.HS_FLAG_MULTILINE
    return source, hs_flags


def _compile(expressions: list[str], flags: list[int]) -> hyperscan.Database:
    db = hyperscan.Database(mode=hyperscan.HS_MODE_BLOCK)
    db.compile(
        expressions=[expr.encode() for expr in expressions],
        ids=list(range(len(expressions))),
        flags=flags,
    )
    return db


_CONTINUATION = bytes(
    1 if _UTF8_CONT_LO <= byte <= _UTF8_CONT_HI else 0 for byte in range(256)
)


def _char_ends(data: bytes, byte_ends: Sequence[int]) -> list[int]:
    """Translate byte end-offsets of UTF-8 ``data`` to character offsets.

    One sweep of the body serves any number of hits.
    """
    out: list[int] = []
    byte_at = 0
    chars_at = 0
    for end in sorted(byte_ends):
        segment = data[byte_at:end]
        chars_at += len(segment) - segment.translate(_CONTINUATION).count(1)
        byte_at = end
        out.append(chars_at)
    return out


# Reversal slice for the exact-path encode: one ``text[::-1]`` of a
# 10 MB body is a single >10 ms C call holding the GIL, which stalls
# the event loop serving the drain; the slices drop the GIL between
# chunks so the loop keeps its cadence.
_REVERSE_CHUNK = 1 << 18


def _reversed_utf8(text: str) -> bytes:
    """UTF-8 bytes of the reversed text, in GIL-yielding chunks.

    Byte-identical to ``text[::-1].encode()``: slices are character-
    wise, so no code point is ever split, and the reversed image of a
    concatenation is the concatenation of the reversed images in
    reverse order.
    """
    if len(text) <= _REVERSE_CHUNK:
        return text[::-1].encode()
    parts = []
    for start in range(0, len(text), _REVERSE_CHUNK):
        parts.append(text[start : start + _REVERSE_CHUNK][::-1].encode())
        time.sleep(0)  # offer the loop thread the GIL between slices
    return b"".join(reversed(parts))


class _Exact:
    """One engine-exact pattern: reversed expression plus divergence tags."""

    __slots__ = ("fold_sensitive", "index", "pattern", "unicode_classes")

    def __init__(
        self,
        index: int,
        pattern: re.Pattern,
        *,
        unicode_classes: bool,
        fold_sensitive: bool,
    ) -> None:
        self.index = index
        self.pattern = pattern
        self.unicode_classes = unicode_classes
        self.fold_sensitive = fold_sensitive


class _Pre:
    """One approximate-prefilter pattern."""

    __slots__ = ("index", "pattern")

    def __init__(self, index: int, pattern: re.Pattern) -> None:
        self.index = index
        self.pattern = pattern


class FastScan:
    """Multi-pattern find-all: patterns in, per-pattern spans out.

    Each span list is identical to the stdlib find-iterator's for
    engine-exact patterns; approximate prefitters report supersets. Build
    one per ruleset (compile once, scan many); the engine releases the GIL
    around its scan calls.
    """

    __slots__ = (
        "_exact",
        "_exact_db",
        "_patterns",
        "_prefilter_db",
        "_prefilters",
    )

    def __init__(
        self,
        patterns: tuple[re.Pattern, ...],
        exact: tuple[_Exact, ...],
        exact_db: hyperscan.Database | None,
        prefilters: tuple[_Pre, ...],
        prefilter_db: hyperscan.Database | None,
    ) -> None:
        """Assemble a scanner from build() results."""
        self._patterns = patterns
        self._exact = exact
        self._exact_db = exact_db
        self._prefilters = prefilters
        self._prefilter_db = prefilter_db

    @classmethod
    def build(cls, patterns: Sequence[re.Pattern]) -> FastScan:
        """Compile patterns into one fast-scan database.

        Patterns the engine refuses compile as approximate prefilters
        (reported by :attr:`prefiltered`). Patterns the engine can neither
        scan exactly nor approximate — zero-width-matching constructs above
        all — raise ``ValueError`` naming the offending pattern.

        Args:
            patterns: compiled patterns, in input order.

        Returns:
            A ready-to-scan :class:`FastScan`.

        Raises:
            TypeError: an item is not a compiled pattern.
            ValueError: a pattern can be neither scanned nor approximated.
        """
        checked = tuple(patterns)
        for pattern in checked:
            if not isinstance(pattern, re.Pattern):
                raise TypeError(
                    "fast-scan patterns must be compiled re.Pattern objects,"
                    f" got {type(pattern).__name__}"
                )
        exact: list[_Exact] = []
        exact_sources: list[str] = []
        exact_flags: list[int] = []
        prefilters: list[_Pre] = []
        prefilter_sources: list[str] = []
        prefilter_flags: list[int] = []
        for index, pattern in enumerate(checked):
            spec = None
            try:
                source, hs_flags, unicode_classes, fold = _reverse_expression(pattern)
            except _UnsupportedError:
                source = None
            if source is not None:
                try:
                    _compile([source], [hs_flags])
                except hyperscan.error:
                    source = None
                else:
                    spec = _Exact(
                        index,
                        pattern,
                        unicode_classes=unicode_classes,
                        fold_sensitive=fold,
                    )
                    exact.append(spec)
                    exact_sources.append(source)
                    exact_flags.append(hs_flags)
            if spec is None:
                pf_source, pf_flags = _prefilter_expression(pattern)
                try:
                    _compile([pf_source], [pf_flags])
                except hyperscan.error as exc:
                    raise ValueError(
                        f"fast-scan cannot compile or approximate pattern"
                        f" {index}: {pattern.pattern!r} ({exc})"
                    ) from exc
                prefilters.append(_Pre(index, pattern))
                prefilter_sources.append(pf_source)
                prefilter_flags.append(pf_flags)
        exact_db = _compile(exact_sources, exact_flags) if exact_sources else None
        prefilter_db = (
            _compile(prefilter_sources, prefilter_flags) if prefilter_sources else None
        )
        return cls(
            checked,
            tuple(exact),
            exact_db,
            tuple(prefilters),
            prefilter_db,
        )

    @property
    def patterns(self) -> tuple[re.Pattern, ...]:
        """The input patterns, in order."""
        return self._patterns

    @property
    def prefiltered(self) -> frozenset[int]:
        """Indices of patterns running as approximate prefilters."""
        return frozenset(spec.index for spec in self._prefilters)

    def find_all(self, text: str) -> list[list[tuple[int, int]]]:
        """All matches of all patterns against ``text``.

        Args:
            text: the decoded body text the Scanner scans (character
                offsets in the results index into this exact string).

        Returns:
            One span list per input pattern, in input order. For
            engine-exact patterns every span equals the stdlib
            find-iterator's. For prefiltered patterns the spans are
            approximation hits — end offsets are real, starts are 0, hits
            may over-report; every body truly matching the pattern reports
            at least one span.
        """
        results: list[list[tuple[int, int]]] = [[] for _ in self._patterns]
        ascii_text = text.isascii()
        if self._prefilter_db is not None:
            self._scan_prefilters(text, ascii_text=ascii_text, results=results)
        if self._exact_db is not None:
            self._scan_exact(text, ascii_text=ascii_text, results=results)
        return results

    def _scan_prefilters(
        self,
        text: str,
        *,
        ascii_text: bool,
        results: list[list[tuple[int, int]]],
    ) -> None:
        data = text.encode()
        ends: list[list[int]] = [[] for _ in self._prefilters]
        self._prefilter_db.scan(
            data,
            lambda expr_id, _start, end, _flags, _ctx: (
                ends[expr_id].append(end),
                0,
            )[1],
        )
        for slot, spec in enumerate(self._prefilters):
            byte_ends = ends[slot] if ascii_text else _char_ends(data, ends[slot])
            spans: list[tuple[int, int]] = sorted({(0, end) for end in byte_ends})
            if not spans and not ascii_text:
                # The engine's Unicode tables are not Python's; when the
                # approximation came back empty on a non-ASCII body, let
                # the true pattern stand in for the superset guarantee.
                match = spec.pattern.search(text)
                if match:
                    spans = [match.span()]
            results[spec.index] = spans

    def _scan_exact(
        self,
        text: str,
        *,
        ascii_text: bool,
        results: list[list[tuple[int, int]]],
    ) -> None:
        length = len(text)
        data = _reversed_utf8(text)
        ends: list[list[int]] = [[] for _ in self._exact]
        self._exact_db.scan(
            data,
            lambda expr_id, _start, end, _flags, _ctx: (
                ends[expr_id].append(end),
                0,
            )[1],
        )
        fold_divergent = not ascii_text and (
            _FOLD_DIVERGENT[0] in text or _FOLD_DIVERGENT[1] in text
        )
        for slot, spec in enumerate(self._exact):
            if (spec.unicode_classes and not ascii_text) or (
                spec.fold_sensitive and fold_divergent
            ):
                results[spec.index] = [
                    match.span() for match in spec.pattern.finditer(text)
                ]
                continue
            char_ends = ends[slot] if ascii_text else _char_ends(data, ends[slot])
            starts = sorted({length - end for end in char_ends})
            pos = 0
            spans: list[tuple[int, int]] = []
            for start in starts:
                if start < pos:
                    continue
                match = spec.pattern.match(text, start)
                if match is None:
                    continue
                end = match.end()
                spans.append((start, end))
                pos = end
            results[spec.index] = spans
