# See ADR-0037.
from __future__ import annotations

import contextlib
import logging
import re
from typing import TYPE_CHECKING, NamedTuple

import hyperscan

from odda.scanner.plumbing import GIL_CHUNK, chunked, sre_constants, sre_parse

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Sequence

_AT_REVERSED = {
    sre_constants.AT_BEGINNING: "$",
    sre_constants.AT_BEGINNING_STRING: r"\z",
    sre_constants.AT_END: "^",
    sre_constants.AT_END_STRING: r"\A",
    sre_constants.AT_BOUNDARY: None,
    sre_constants.AT_NON_BOUNDARY: None,
}
_CATEGORY = {
    sre_constants.CATEGORY_DIGIT: r"\d",
    sre_constants.CATEGORY_NOT_DIGIT: r"\D",
    sre_constants.CATEGORY_SPACE: r"\s",
    sre_constants.CATEGORY_NOT_SPACE: r"\S",
    sre_constants.CATEGORY_WORD: r"\w",
    sre_constants.CATEGORY_NOT_WORD: r"\W",
}
_AT_STRAIGHT = {
    sre_constants.AT_BEGINNING: "^",
    sre_constants.AT_BEGINNING_STRING: r"\A",
    sre_constants.AT_END: "$",
    sre_constants.AT_END_STRING: r"\z",
    sre_constants.AT_BOUNDARY: r"\b",
    sre_constants.AT_NON_BOUNDARY: r"\B",
}

_PREFILTER_REPEAT_CEILING = 64

_UNI_CLASS_CATEGORIES = frozenset(_CATEGORY)
_FOLD_DIVERGENT = "\u0130\u0131"
_META_OUTSIDE = set("\\^$.|?*+()[]{}")
_META_IN_CLASS = set("\\]^-")
_ASCII_CTRL = 0x20
_ASCII_DEL = 0x7F
_UTF8_CONT_LO = 0x80
_UTF8_CONT_HI = 0xBF

_RE_LEADING_GLOBAL_FLAGS = re.compile(r"^\(\?[aiLmsux]+\)")


class _UnsupportedError(Exception):
    pass


def _escape(ch: str, *, in_class: bool = False) -> str:
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
        if op is sre_constants.NEGATE:
            negate = "^"
        elif op is sre_constants.LITERAL:
            parts.append(_escape(chr(av), in_class=True))
        elif op is sre_constants.RANGE:
            parts.append(
                _escape(chr(av[0]), in_class=True)
                + "-"
                + _escape(chr(av[1]), in_class=True)
            )
        elif op is sre_constants.CATEGORY:
            parts.append(_CATEGORY[av])
        else:
            raise _UnsupportedError(f"character-class item {op}")
    return f"[{negate}{''.join(parts)}]"


def _min_length(tree) -> int:
    total = 0
    for op, av in tree:
        if op in (
            sre_constants.LITERAL,
            sre_constants.IN,
            sre_constants.ANY,
            sre_constants.NOT_LITERAL,
        ):
            total += 1
        elif op is sre_constants.SUBPATTERN:
            total += _min_length(av[3])
        elif op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT):
            total += av[0] * _min_length(av[2])
        elif op is sre_constants.BRANCH:
            total += min(_min_length(branch) for branch in av[1])
        elif op is sre_constants.AT:
            continue
        else:
            raise _UnsupportedError(f"construct {op}")
    return total


def _reversed_repeat(av) -> str:
    mn, mx, sub = av
    body, atom = _reversed_single(sub)
    if not atom:
        body = f"(?:{body})"
    if mx == sre_constants.MAXREPEAT:
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
    op, av = node
    if op is sre_constants.LITERAL:
        return _escape(chr(av)), True
    if op is sre_constants.IN:
        return _class_source(av), True
    if op is sre_constants.ANY:
        return ".", True
    if op is sre_constants.NOT_LITERAL:
        return f"[^{_escape(chr(av), in_class=True)}]", True
    if op is sre_constants.AT:
        token = _AT_REVERSED[av]
        if token is None:
            raise _UnsupportedError(f"anchor {av}")
        return token, True
    if op is sre_constants.SUBPATTERN:
        return f"(?:{_reversed_source(av[3])})", True
    if op is sre_constants.BRANCH:
        alts = "|".join(_reversed_source(branch) for branch in av[1])
        return f"(?:{alts})", True
    if op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT):
        return _reversed_repeat(av), True
    raise _UnsupportedError(f"construct {op}")


def _reversed_single(tree) -> tuple[str, bool]:
    items = list(tree)
    if len(items) == 1:
        return _reversed_item(items[0])
    return _reversed_source(items), False


def _reversed_source(tree) -> str:
    items = [_reversed_item(node) for node in tree]
    return "".join(source for source, _ in reversed(items))


def _prefilter_repeat(av) -> str:
    # See ADR-0037.
    mn, mx, sub = av
    body, atom = _prefilter_single(sub)
    if not atom:
        body = f"(?:{body})"
    if mx == sre_constants.MAXREPEAT or mx >= _PREFILTER_REPEAT_CEILING:
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


def _scoped_flags_source(add: int, del_: int) -> str:
    letters = {
        re.IGNORECASE: "i",
        re.DOTALL: "s",
        re.MULTILINE: "m",
    }
    parts = []
    for flags, joiner in ((add, ""), (del_, "-")):
        bits = "".join(letter for bit, letter in letters.items() if flags & bit)
        rest = flags & ~(re.IGNORECASE | re.DOTALL | re.MULTILINE)
        if rest:
            raise _UnsupportedError(f"scoped flag {rest}")
        if bits:
            parts.append(joiner + bits)
    return "".join(parts)


def _prefilter_atom(node) -> tuple[str, bool]:
    op, av = node
    if op is sre_constants.LITERAL:
        return _escape(chr(av)), True
    if op is sre_constants.IN:
        return _class_source(av), True
    if op is sre_constants.ANY:
        return ".", True
    if op is sre_constants.NOT_LITERAL:
        return f"[^{_escape(chr(av), in_class=True)}]", True
    return f"\\{av}", True


def _prefilter_grouping(node) -> tuple[str, bool]:
    op, av = node
    if op is sre_constants.SUBPATTERN:
        group, add_flags, del_flags, sub = av
        body = _prefilter_source(sub)
        if add_flags or del_flags:
            letters = _scoped_flags_source(add_flags, del_flags)
            return f"(?{letters}:{body})", True
        if group is not None:
            return f"({body})", True
        return f"(?:{body})", True
    if op is sre_constants.ATOMIC_GROUP:
        return f"(?:{_prefilter_source(av)})", True
    direction, sub = av
    kind = "=" if op is sre_constants.ASSERT else "!"
    arrow = "<" if direction < 0 else ""
    return f"(?{arrow}{kind}{_prefilter_source(sub)})", True


def _prefilter_item(node) -> tuple[str, bool]:
    op, av = node
    if op in (
        sre_constants.LITERAL,
        sre_constants.IN,
        sre_constants.ANY,
        sre_constants.NOT_LITERAL,
        sre_constants.GROUPREF,
    ):
        return _prefilter_atom(node)
    if op is sre_constants.AT:
        token = _AT_STRAIGHT.get(av)
        if token is None:
            raise _UnsupportedError(f"anchor {av}")
        return token, True
    if op in (
        sre_constants.SUBPATTERN,
        sre_constants.ATOMIC_GROUP,
        sre_constants.ASSERT,
        sre_constants.ASSERT_NOT,
    ):
        return _prefilter_grouping(node)
    if op is sre_constants.BRANCH:
        alts = "|".join(_prefilter_source(branch) for branch in av[1])
        return f"(?:{alts})", True
    if op in (
        sre_constants.MAX_REPEAT,
        sre_constants.MIN_REPEAT,
        sre_constants.POSSESSIVE_REPEAT,
    ):
        return _prefilter_repeat(av), True
    raise _UnsupportedError(f"construct {op}")


def _prefilter_single(tree) -> tuple[str, bool]:
    items = list(tree)
    if len(items) == 1:
        return _prefilter_item(items[0])
    return _prefilter_source(items), False


def _prefilter_source(tree) -> str:
    return "".join(_prefilter_item(node)[0] for node in tree)


def _has_wide_repeat(tree) -> bool:
    found = False

    def check(node) -> None:
        nonlocal found
        op, av = node
        if (
            op
            in (
                sre_constants.MAX_REPEAT,
                sre_constants.MIN_REPEAT,
                sre_constants.POSSESSIVE_REPEAT,
            )
            and av[1] >= _PREFILTER_REPEAT_CEILING
        ):
            found = True

    _walk_tree(tree, check)
    return found


def _walk_tree(tree, fn) -> None:
    for node in tree:
        fn(node)
        op, av = node
        if op is sre_constants.SUBPATTERN:
            _walk_tree(av[3], fn)
        elif op is sre_constants.BRANCH:
            for branch in av[1]:
                _walk_tree(branch, fn)
        elif op in (
            sre_constants.MAX_REPEAT,
            sre_constants.MIN_REPEAT,
            sre_constants.POSSESSIVE_REPEAT,
        ):
            _walk_tree(av[2], fn)
        elif op is sre_constants.ATOMIC_GROUP:
            _walk_tree(av, fn)


def _inline_flags(tree) -> int:
    flags = 0

    def collect(node) -> None:
        nonlocal flags
        op, av = node
        if op is sre_constants.SUBPATTERN and av[1]:
            flags |= av[1]

    _walk_tree(tree, collect)
    return flags


def _contains_at_end(tree) -> bool:
    found = False

    def check(node) -> None:
        nonlocal found
        op, av = node
        if op is sre_constants.AT and av is sre_constants.AT_END:
            found = True

    _walk_tree(tree, check)
    return found


def _uses_unicode_class(tree) -> bool:
    uses = False

    def check(node) -> None:
        nonlocal uses
        op, av = node
        if op is sre_constants.IN and any(
            item[0] is sre_constants.CATEGORY and item[1] in _UNI_CLASS_CATEGORIES
            for item in av
        ):
            uses = True

    _walk_tree(tree, check)
    return uses


def _reverse_expression(pattern: re.Pattern) -> tuple[str, int, bool, bool]:
    flags = pattern.flags
    tree = sre_parse(pattern.pattern, flags)
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
    flags = pattern.flags
    source = _RE_LEADING_GLOBAL_FLAGS.sub("", pattern.pattern, count=1)
    tree = sre_parse(pattern.pattern, flags)
    if _has_wide_repeat(tree):
        with contextlib.suppress(_UnsupportedError):
            source = _prefilter_source(tree)
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
    out: list[int] = []
    byte_at = 0
    chars_at = 0
    for end in sorted(byte_ends):
        segment = data[byte_at:end]
        chars_at += len(segment) - segment.translate(_CONTINUATION).count(1)
        byte_at = end
        out.append(chars_at)
    return out


def _reversed_utf8(text: str) -> bytes | bytearray:
    if len(text) <= GIL_CHUNK:
        return text[::-1].encode()
    parts = [part[::-1].encode() for part in chunked(text)]
    data = bytearray(sum(len(part) for part in parts))
    pos = 0
    for group in chunked(parts[::-1], 1):
        part = group[0]
        data[pos : pos + len(part)] = part
        pos += len(part)
    return data


def _confirmed_spans(pattern: re.Pattern, text: str) -> list[tuple[int, int]]:
    # See ADR-0037.
    return [match.span() for match in pattern.finditer(text)]


class _Exact:
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
    __slots__ = ("index", "pattern")

    def __init__(self, index: int, pattern: re.Pattern) -> None:
        self.index = index
        self.pattern = pattern


class _Fate(NamedTuple):
    exact: _Exact | None
    exact_source: str | None
    exact_flags: int
    prefilter: _Pre | None
    prefilter_source: str | None
    prefilter_flags: int
    refusal: ValueError | None


def _classify_pattern(index: int, pattern: re.Pattern) -> _Fate:
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
            return _Fate(
                _Exact(
                    index,
                    pattern,
                    unicode_classes=unicode_classes,
                    fold_sensitive=fold,
                ),
                source,
                hs_flags,
                None,
                None,
                0,
                None,
            )
    pf_source, pf_flags = _prefilter_expression(pattern)
    try:
        _compile([pf_source], [pf_flags])
    except hyperscan.error as exc:
        return _Fate(
            None,
            None,
            0,
            None,
            None,
            0,
            ValueError(
                f"fast-scan cannot compile or approximate pattern"
                f" {index}: {pattern.pattern!r} ({exc})"
            ),
        )
    return _Fate(None, None, 0, _Pre(index, pattern), pf_source, pf_flags, None)


class FastScan:
    # See ADR-0037.

    __slots__ = (
        "_exact",
        "_exact_db",
        "_patterns",
        "_prefilter_db",
        "_prefilters",
        "_refused",
    )

    def __init__(
        self,
        patterns: tuple[re.Pattern, ...],
        exact: tuple[_Exact, ...],
        exact_db: hyperscan.Database | None,
        prefilters: tuple[_Pre, ...],
        prefilter_db: hyperscan.Database | None,
        refused: frozenset[int] = frozenset(),
    ) -> None:
        self._patterns = patterns
        self._exact = exact
        self._exact_db = exact_db
        self._prefilters = prefilters
        self._prefilter_db = prefilter_db
        self._refused = refused

    @classmethod
    def build(cls, patterns: Sequence[re.Pattern]) -> FastScan:
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
        refused: list[int] = []
        refusal: ValueError | None = None
        for index, pattern in enumerate(checked):
            fate = _classify_pattern(index, pattern)
            if fate.refusal is not None:
                refusal = fate.refusal
                refused.append(index)
            elif fate.exact is not None:
                exact.append(fate.exact)
                exact_sources.append(fate.exact_source or "")
                exact_flags.append(fate.exact_flags)
            else:
                prefilters.append(fate.prefilter)
                prefilter_sources.append(fate.prefilter_source or "")
                prefilter_flags.append(fate.prefilter_flags)
        if refusal is not None and not exact and not prefilters:
            raise refusal
        try:
            exact_db = _compile(exact_sources, exact_flags) if exact_sources else None
            prefilter_db = (
                _compile(prefilter_sources, prefilter_flags)
                if prefilter_sources
                else None
            )
        except hyperscan.error:
            logger.exception("fast-scan combined compile failed; every pattern refused")
            return cls(checked, (), None, (), None, frozenset(range(len(checked))))
        return cls(
            checked,
            tuple(exact),
            exact_db,
            tuple(prefilters),
            prefilter_db,
            frozenset(refused),
        )

    @property
    def patterns(self) -> tuple[re.Pattern, ...]:
        return self._patterns

    @property
    def prefiltered(self) -> frozenset[int]:
        return frozenset(spec.index for spec in self._prefilters)

    @property
    def refused(self) -> frozenset[int]:
        return self._refused

    def find_all(self, text: str) -> list[list[tuple[int, int]]]:
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
            if ends[slot]:
                results[spec.index] = _confirmed_spans(spec.pattern, text)
            elif not ascii_text:
                confirmed = _confirmed_spans(spec.pattern, text)
                if confirmed:
                    results[spec.index] = confirmed

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
