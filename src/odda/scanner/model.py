"""Stable rule-author surface for the passive scanner (ADR-0035).

Rules are typed Python registrations, not a DSL: rule files import
these types and expose a module-level ``RULES`` list. This module is
the contract rule authors build against — the surface stays additive
across odda upgrades (new optional fields, never renames).

The match spec is declarative: regex patterns matched against the
flow's stored material, with gates that restrict which flows a rule
sees. A correlator seam exists for future cross-flow analysis; none
ship.
"""

from __future__ import annotations

import codecs
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol
from urllib.parse import parse_qsl, urlsplit

if TYPE_CHECKING:
    from pathlib import Path

try:  # private API, but the only parser shipped; a missing module
    # just turns every gate off (patterns scan ungated, as before).
    from re import _constants as _re_constants
    from re._parser import parse as _re_parse
except ImportError:  # pragma: no cover
    _re_constants = None  # type: ignore[assignment]
    _re_parse = None  # type: ignore[assignment]


class Severity(StrEnum):
    """Five-level triage scale; findings carry the value string."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class Evidence:
    """Where inside a flow one match landed, with the matched excerpt.

    Attributes:
        where: Which part of the flow matched (``response_body``,
            ``response_headers``, ``request_query``,
            ``request_headers``).
        offset: Character offset into that part for text material;
            the item's index for the request-side lists (query
            parameters, request headers).
        detail: The matched text, bounded to a greppable excerpt.
    """

    where: str
    offset: int
    detail: str


@dataclass(frozen=True, slots=True)
class Finding:
    """One rule matching one flow — the findings.jsonl record.

    Identity is the (rule id, flow id) pair: all matches inside one
    flow aggregate into one finding's evidence. Findings are never
    retracted; removing a rule stops future findings only.
    """

    rule_id: str
    flow_id: str
    severity: Severity
    title: str
    evidence: tuple[Evidence, ...]
    refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Rule:
    """One passive check over one flow's captured material.

    Attributes:
        id: Permanent rule identifier (keys findings across the
            append-only index; never rename a shipped rule).
        severity: Triage level carried onto every finding.
        title: Human-readable one-liner for the finding record.
        body_patterns: Regexes matched against the stored response
            body; each match becomes one evidence entry. Plain strings
            compile with ``re.MULTILINE`` (``^``/``$`` match per line,
            which frame-shaped patterns need); precompiled patterns
            pass through untouched.
        header_patterns: Regexes matched against the stored raw
            response headers (status line + header lines); each match
            becomes one evidence entry with
            ``where="response_headers"``. Compile semantics match
            ``body_patterns``.
        request_query_patterns: Regexes matched against the request
            URL's query parameter *names*; each matching parameter
            becomes one evidence entry with
            ``where="request_query"`` and the ``name=value`` pair as
            detail. Compile semantics match ``body_patterns``.
        request_header_patterns: Regexes matched against the stored
            request header lines (``"Name: value"``); each match
            becomes one evidence entry with
            ``where="request_headers"``. Compile semantics match
            ``body_patterns``.
        schemes: Gate — schemes the rule applies to (``("http",)``);
            empty matches all.
        statuses: Gate — response status codes the rule applies to;
            empty matches all. Error flows (no response) never pass a
            status gate.
        content_types: Gate — content-type prefixes the rule applies
            to (``("text/",)`` matches ``text/html``); empty matches
            all. Flows without a response have no content type and
            pass no content-type gate.
        browser_only: Gate — restrict to flows that rode a browser's
            proxy credentials (``browser_id`` set), so crafted
            ``request_send`` probes are structurally invisible.
        request_browser_only: Gate — like ``browser_only`` but scoped
            to the request-side matchers: with it set,
            ``request_query_patterns`` and ``request_header_patterns``
            apply to browser-attributed flows only while the
            response-side matchers stay ungated. The mixed shape one
            rule otherwise could not express (the Xdebug rule: the
            Set-Cookie echo ungated, the request-param half gated).
        refs: CWE/CVE/OSV identifiers or URLs carried onto findings.
    """

    id: str
    severity: Severity
    title: str
    body_patterns: tuple[re.Pattern[str] | str, ...] = ()
    header_patterns: tuple[re.Pattern[str] | str, ...] = ()
    request_query_patterns: tuple[re.Pattern[str] | str, ...] = ()
    request_header_patterns: tuple[re.Pattern[str] | str, ...] = ()
    schemes: tuple[str, ...] = ()
    statuses: tuple[int, ...] = ()
    content_types: tuple[str, ...] = ()
    browser_only: bool = False
    request_browser_only: bool = False
    refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Compile string patterns (MULTILINE) so the engine never recompiles."""
        object.__setattr__(self, "body_patterns", _compile(self.body_patterns))
        object.__setattr__(self, "header_patterns", _compile(self.header_patterns))
        object.__setattr__(
            self, "request_query_patterns", _compile(self.request_query_patterns)
        )
        object.__setattr__(
            self, "request_header_patterns", _compile(self.request_header_patterns)
        )

    def scan(self, view: FlowView) -> list[Finding] | None:  # noqa: ARG002
        """Declarative rules return ``None``; dataset-backed rules override."""
        return None


def _compile(
    patterns: tuple[re.Pattern[str] | str, ...],
) -> tuple[re.Pattern[str], ...]:
    """Compile plain strings with ``re.MULTILINE``; patterns pass through."""
    return tuple(
        p if isinstance(p, re.Pattern) else re.compile(p, re.MULTILINE)
        for p in patterns
    )


# --- necessary-literal gates (ticket 08) ---
#
# A body-regex scan is one C call that holds the GIL for its whole
# duration, so with scans off the event-loop thread (engine.run) a
# multi-MB body still turns each unmatched pattern into a
# tens-to-hundreds-of-ms loop stall. The gate below restores
# interactivity: per pattern, a tuple of literal sets such that any
# match must put at least one literal of each set into the scanned
# text — spine literals as singleton sets, one any-of set per
# alternation. A failing ``in`` check (fastsearch speed) replaces a
# linear regex scan for free.
#
# Soundness is conservative by construction: case-insensitive
# patterns (global or scoped), character classes, zero-width and
# optional constructs contribute nothing, so a pattern without a
# usable literal simply has no gate and scans ungated. The fuzz test
# in tests/e2e/test_30_scanner.py pins the property that matters: a
# real match is never gated out.

_GATE_MIN_LITERAL = 3


# Slice length for the GIL-cadenced text ops below: one 10 MB pass is
# a single uninterruptible C call that stalls the event loop serving
# the drain for its whole duration; the slices drop the GIL between
# chunks, so the loop keeps its cadence whatever the body size
# (ticket 12's numbers contract).
_GIL_CHUNK = 1 << 18


def _fold_chunk(text: str) -> str:
    """One fold slice.

    Case folding is per character, so slicing is exact: folding a
    concatenation concatenates the folds.
    """
    return text.casefold().replace("\u0307", "").replace("\u0131", "i")


def _gate_fold(text: str) -> str:
    """The fold the case-insensitive gates check against, chunked.

    Large texts fold in GIL-yielding chunks (see ``_GIL_CHUNK``).
    Full case folding normalized to Python's per-character (?i)
    classes: folding expands U+0130 to ``i`` + U+0307 (dot stripped)
    and leaves U+0131 distinct, while Python's (?i) matches both
    against ``i`` — normalizing each to ``i`` keeps a folded literal a
    substring of the folded text for every real match (the sweep in
    tests/e2e/test_30_scanner.py pins the per-character edge).
    """
    if len(text) <= _GIL_CHUNK:
        return _fold_chunk(text)
    parts = []
    for start in range(0, len(text), _GIL_CHUNK):
        parts.append(_fold_chunk(text[start : start + _GIL_CHUNK]))
        time.sleep(0)  # offer the loop thread the GIL between slices
    return "".join(parts)


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
    if span >= _GIL_CHUNK:  # pathological literal: windows cannot cover it
        folded = _gate_fold(text)
        return all(any(lit in folded for lit in s) for s in gates)
    pending = list(gates)
    carry = ""
    for start in range(0, len(text), _GIL_CHUNK):
        folded = carry + _fold_chunk(text[start : start + _GIL_CHUNK])
        pending = [s for s in pending if not any(lit in folded for lit in s)]
        if not pending:
            return True
        carry = folded[-span:] if span else ""
        time.sleep(0)  # offer the loop thread the GIL between slices
    return not pending


def _gate_contains(literal: str, text: str) -> bool:
    """Chunked substring containment (the gate scans' one linear op).

    Windows overlap by the literal's length, so an occurrence spanning
    a boundary is never missed; each slice is one short C call with a
    GIL drop after it, instead of one call over the whole body.
    """
    if len(text) <= _GIL_CHUNK or len(literal) >= _GIL_CHUNK:
        return literal in text
    step = _GIL_CHUNK - len(literal) + 1
    for start in range(0, len(text), step):
        if literal in text[start : start + _GIL_CHUNK]:
            return True
        time.sleep(0)  # offer the loop thread the GIL between slices
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
    if _re_parse is None:
        return None
    try:
        tree = _re_parse(source, flags)
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
    if op is _re_constants.SUBPATTERN:
        # av = (group, add_flags, del_flags, sub). Scoped inline flags
        # ((?i:...)) make the group's literals case insensitive — not
        # sound as case-sensitive gates, so a flagged group stays out.
        if not av[1] & re.IGNORECASE:
            out.extend(_concat_gates(av[-1]))
    elif op in (_re_constants.MAX_REPEAT, _re_constants.MIN_REPEAT):
        if av[0] >= 1:  # required at least once: in every match
            out.extend(_concat_gates(av[-1]))
    elif op is _re_constants.BRANCH:
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
        if op is _re_constants.LITERAL:
            run += chr(av)
            continue
        if len(run) >= _GATE_MIN_LITERAL:
            out.append(frozenset((run,)))
        run = ""
        _dispatch_gates(op, av, out)
    if len(run) >= _GATE_MIN_LITERAL:
        out.append(frozenset((run,)))
    return out


def _passes_literal_gates(pattern: re.Pattern[str], text: str) -> bool:
    """Whether ``pattern`` should run on ``text``: no gate, or all pass.

    A globally case-insensitive pattern checks its folded literals
    against the casefolded text (one fold per call — the scanned
    texts are tiny or already off the event loop's thread).
    """
    gates = _literal_gates(pattern.pattern, pattern.flags)
    if gates is None:
        return True
    if pattern.flags & re.IGNORECASE:
        if len(text) > _GIL_CHUNK:
            return _ci_gates_pass(gates, text)
        text = _gate_fold(text)
    return all(any(_gate_contains(lit, text) for lit in s) for s in gates)


class Correlator(Protocol):
    """Cross-flow analysis seam: observes flows, may emit findings.

    Where a rule sees one flow in isolation, a correlator keeps state
    across observations and reports patterns over time (findings it
    returns reference the flow that triggered them). Zero correlators
    ship; the seam exists so cross-flow checks arrive without another
    interface change.
    """

    id: str

    def observe(self, view: FlowView) -> list[Finding]:
        """Observe one flow view; return findings to record (if any)."""
        ...


class _ParsedRequest(NamedTuple):
    """The request line and headers of one stored request, parsed once.

    Fields:
        method: The request line's method token, or ``None`` when the
            line carries none.
        path: The request line's target (origin-form path with query,
            per the request-file contract), or ``None`` when absent.
        query: Decoded ``(name, value)`` pairs of the target's query
            string, in order.
        headers: ``(name, value)`` pairs of the header lines, verbatim
            case and order, duplicates kept (they are legal and the
            stored file records them faithfully).
    """

    method: str | None
    path: str | None
    query: tuple[tuple[str, str], ...]
    headers: tuple[tuple[str, str], ...]


class FlowView:
    """The captured material of one flow, as rules receive it.

    Backed by the flow's stored files; each property is read once on
    first use, so a response-side rule never pays for the request file
    and a request-side rule never pays for the response side. Decoding
    is lenient (``errors="replace"``) — stored bodies may be binary.
    Reading a file that does not exist (an error flow has no response
    headers; a rule asking for material the flow never had) raises
    :class:`FileNotFoundError` — the engine contains it per rule.

    Attributes:
        record: The flows.jsonl record fields (id, method, scheme,
            host, port, path, browser_id, status_code, body_file, ...).
    """

    def __init__(self, data_dir: Path, record: dict[str, Any]) -> None:
        """Initialize from the data dir and the flow's index record.

        Args:
            data_dir: The session's ``.odda`` directory.
            record: The flows.jsonl record for this flow.
        """
        self.record = record
        self._data_dir = data_dir
        self._flow_dir = data_dir / "flows" / str(record["id"])
        self._request: str | None = None
        self._response_headers: str | None = None
        self._body: str | None = None
        self._body_bytes: bytes | None = None
        self._content_type: str | None = None
        self._parsed_request: _ParsedRequest | None = None

    @property
    def request(self) -> str:
        """The flow's stored raw request (request line + headers + body)."""
        if self._request is None:
            self._request = self._read(self._flow_dir / "request")
        return self._request

    @property
    def request_method(self) -> str | None:
        """The request line's method token (``GET``), or ``None``."""
        return self._parse_request().method

    @property
    def request_path(self) -> str | None:
        """The request line's target (path with query), or ``None``."""
        return self._parse_request().path

    @property
    def request_query(self) -> tuple[tuple[str, str], ...]:
        """Decoded ``(name, value)`` pairs of the request URL's query."""
        return self._parse_request().query

    @property
    def request_headers(self) -> tuple[tuple[str, str], ...]:
        """``(name, value)`` pairs of the request's header lines, in order."""
        return self._parse_request().headers

    def _parse_request(self) -> _ParsedRequest:
        """Parse the stored request once; the head only, never the body.

        The blank line ends the head, so request-header matching can
        never reach into the request body; a request line without a
        target or a header line without a colon degrades to ``None``
        / is dropped rather than raising (captured material, not a
        parser's opinion, is the contract).
        """
        if self._parsed_request is None:
            head = self.request.split("\r\n\r\n", 1)[0]
            lines = head.split("\r\n")
            parts = lines[0].split(" ")
            method = parts[0] or None
            path = parts[1] if len(parts) > 1 else None
            headers = tuple(
                (name, value.strip())
                for name, value in (
                    line.split(":", 1) for line in lines[1:] if ":" in line
                )
            )
            query = tuple(parse_qsl(urlsplit(path or "").query))
            self._parsed_request = _ParsedRequest(method, path, query, headers)
        return self._parsed_request

    @property
    def response_headers(self) -> str:
        """The flow's stored response headers (status line + headers)."""
        if self._response_headers is None:
            self._response_headers = self._read(self._flow_dir / "response_headers")
        return self._response_headers

    @property
    def body(self) -> str | None:
        """The stored response body, or ``None`` when none was stored.

        Large bodies read and decode in GIL-dropping chunks (see
        ``_GIL_CHUNK``). The one-shot lenient decode is a single
        state-machine pass over the whole body — the most expensive
        whole-body C call on this path — while the chunked shape pays
        only its final str-join memcpy (~half the cost, measured) as
        the single uninterruptible op, then hands the loop the GIL
        before the next whole-body op can stack behind it.
        """
        if self._body is None:
            body_file = self.record.get("body_file")
            if body_file is not None:
                self._body = self._read_chunked(self._data_dir / body_file)
        return self._body

    @property
    def body_bytes(self) -> bytes | None:
        """The stored response body as raw bytes, or ``None``.

        The bytes-exact view of :attr:`body` for checks that must not
        pass through text decoding — content hashing above all.
        """
        if self._body_bytes is None:
            body_file = self.record.get("body_file")
            if body_file is not None:
                self._body_bytes = (self._data_dir / body_file).read_bytes()
        return self._body_bytes

    @property
    def content_type(self) -> str | None:
        """The response ``Content-Type`` header value, or ``None``.

        ``None`` when the flow has no response headers file (error
        flows) or the header is absent — content-type-gated rules skip
        such flows rather than error.
        """
        if self._content_type is None:
            headers = self._flow_dir / "response_headers"
            if headers.is_file():
                for line in self.response_headers.splitlines():
                    if line.lower().startswith("content-type:"):
                        self._content_type = line.split(":", 1)[1].strip()
                        break
        return self._content_type

    @property
    def is_browser(self) -> bool:
        """Whether the flow rode a browser's proxy credentials."""
        return self.record.get("browser_id") is not None

    @staticmethod
    def _read(path: Path) -> str:
        """Read a stored flow file, decoded leniently (binary-safe)."""
        return path.read_bytes().decode("utf-8", errors="replace")

    @staticmethod
    def _read_chunked(path: Path) -> str:
        """Read and leniently decode a stored body in GIL-dropping chunks."""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        parts: list[str] = []
        with path.open("rb") as handle:
            while chunk := handle.read(_GIL_CHUNK):
                parts.append(decoder.decode(chunk))
                time.sleep(0)  # offer the loop thread the GIL between slices
        parts.append(decoder.decode(b"", final=True))
        body = "".join(parts)
        # The join is the one whole-body copy this path cannot avoid;
        # hand the loop the GIL right after it instead of stacking the
        # next whole-body op behind it inside one GIL hold.
        time.sleep(0)
        return body
