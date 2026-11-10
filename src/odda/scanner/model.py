"""Stable rule-author surface for the passive scanner (ADR-0035).

Rules are typed Python registrations, not a DSL: rule files import
these types and expose a module-level ``RULES`` list. This module is
the contract rule authors build against — the surface stays additive
across odda upgrades (new optional fields, never renames).

The match spec is declarative: regex patterns matched against the
flow's stored material, with gates that restrict which flows a rule
sees.
"""

from __future__ import annotations

import codecs
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple
from urllib.parse import parse_qsl, urlsplit

from odda.scanner.plumbing import read_chunked

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


class Severity(StrEnum):
    """Five-level triage scale; findings carry the value string.

    Definition order is the triage order; :attr:`rank` exposes it for
    aggregates (a dataset rule's highest matched severity wins).
    """

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        """Triage rank, 0 (info) through 4 (critical); higher outranks."""
        return _SEVERITY_RANK[self]


# Derived from the definition order above — the scale's single source.
_SEVERITY_RANK = {severity: rank for rank, severity in enumerate(Severity)}


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

    def body_corpus(self) -> tuple[re.Pattern[str], ...]:
        """Body patterns the engine scans on this rule's behalf.

        Dataset-backed rules (``scan`` overrides) declare here the
        body patterns they would otherwise scan themselves: the engine
        compiles them into the epoch's one body-stage database and
        hands each pattern's confirmed spans back through ``scan``'s
        ``body_spans`` argument. Patterns the engine refuses (over
        budget, zero-width-capable) arrive as ``None`` entries — scan
        them inside ``scan``, behind their literal gates.
        """
        return ()

    def scan(
        self,
        view: FlowView,  # noqa: ARG002
        body_spans: tuple[Sequence[tuple[int, int]] | None, ...] | None = None,  # noqa: ARG002
    ) -> list[Finding] | None:
        """Declarative rules return ``None``; dataset-backed rules override.

        ``body_spans`` aligns with :meth:`body_corpus` — one entry per
        declared pattern: its confirmed spans over the flow's body
        (the stdlib find-iterator's own, engine-exact and approximated
        alike), or ``None`` when that pattern did not ride the engine
        pass and must be scanned here. The engine passes the argument
        exactly when the rule declares a corpus — a plain
        ``scan(view)`` override never sees it. Returning ``None``
        hands the flow to the
        declared matchers and gates; returning a list — empty included
        — is authoritative.
        """
        return None


def _compile(
    patterns: tuple[re.Pattern[str] | str, ...],
) -> tuple[re.Pattern[str], ...]:
    """Compile plain strings with ``re.MULTILINE``; patterns pass through."""
    return tuple(
        p if isinstance(p, re.Pattern) else re.compile(p, re.MULTILINE)
        for p in patterns
    )


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


# The record fields inside the dataset facet (see
# ``FlowView.dataset_facet``): the stable routing fields a scan()
# override may gate on. ``id`` is among them because the memo
# re-stamps it per flow on replay; ``body_file`` is not — only its
# extension, the stored body's content addressing, is facet context.
_DATASET_RECORD_FIELDS = (
    "id",
    "method",
    "scheme",
    "host",
    "port",
    "path",
    "browser_id",
    "status_code",
    "error",
)


class _DatasetRecord(dict):
    """The index-record mapping a tracked dataset scan reads through.

    A plain ``dict`` of the record, except that ``__getitem__`` and
    ``get`` record which fields were read while a dataset scan's
    reads are being collected (``FlowView.dataset_scan_reads``) — a
    field beyond the facet marks the scan unmemoizable.
    """

    __slots__ = ("_view",)

    def __init__(self, record: dict[str, Any], view: FlowView) -> None:
        """Wrap ``record`` for ``view``."""
        super().__init__(record)
        self._view = view

    def __getitem__(self, key: Any) -> Any:
        self._view._note(f"record.{key}")  # noqa: SLF001 — the facet's own log
        return dict.__getitem__(self, key)

    def get(self, key: Any, default: Any = None) -> Any:
        self._view._note(f"record.{key}")  # noqa: SLF001 — the facet's own log
        return dict.get(self, key, default)


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

    Class attributes:
        DATASET_SCAN_READS: The FlowView members a ``scan()``
            override may lawfully read (the dataset facet plus the
            body itself) — a tracked scan that reads anything else is
            never memoized.
    """

    DATASET_SCAN_READS: ClassVar[frozenset[str]] = frozenset(
        {"body", "body_bytes", "content_type", "response_headers", "is_browser"}
        | {f"record.{field}" for field in _DATASET_RECORD_FIELDS}
    )

    def __init__(self, data_dir: Path, record: dict[str, Any]) -> None:
        """Initialize from the data dir and the flow's index record.

        Args:
            data_dir: The session's ``.odda`` directory.
            record: The flows.jsonl record for this flow.
        """
        self._reads: set[str] | None = None
        self._record = record
        self._record_view = _DatasetRecord(record, self)
        self._data_dir = data_dir
        self._flow_dir = data_dir / "flows" / str(record["id"])
        self._request: str | None = None
        self._response_headers: str | None = None
        self._body: str | None = None
        self._body_bytes: bytes | None = None
        self._content_type: str | None = None
        self._parsed_request: _ParsedRequest | None = None

    @property
    def record(self) -> dict[str, Any]:
        """The flows.jsonl record fields, read-tracked in a scan."""
        return self._record_view

    def _note(self, member: str) -> None:
        """Record one member read, when a dataset scan is tracking."""
        if self._reads is not None:
            self._reads.add(member)

    def dataset_facet(self) -> tuple[Any, ...]:
        """The flow context a ``scan()`` override may lawfully gate on.

        This is THE surface beyond the stored body itself that a
        dataset scan may read per flow: the record's stable routing
        fields, the stored body's content addressing (the body file's
        extension), and the stored response headers — the response
        content type above all, a dataset rule's own screen. The
        engine keys the scan-result memo on exactly this tuple (with
        the ruleset epoch and the body digest), so two flows that
        disagree on any of it never share a cached scan. A scan that
        reads beyond the facet — request-side material above all — is
        never memoized: it runs fresh on every flow, by mechanism,
        not convention. The scan() sentinel convention rides the same
        boundary: returning ``None`` hands the flow to the declared
        matchers; a returned list, empty included, is authoritative.
        """
        reads = self._reads
        self._reads = None  # the facet itself is engine context, not rule reads
        try:
            record = self._record_view
            body_file = str(record.get("body_file") or "")
            headers_file = self._flow_dir / "response_headers"
            stored_headers = (
                self._read(headers_file) if headers_file.is_file() else None
            )
            return (
                record.get("method"),
                record.get("scheme"),
                record.get("host"),
                record.get("port"),
                record.get("path"),
                record.get("browser_id"),
                record.get("status_code"),
                record.get("error"),
                Path(body_file).suffix,
                stored_headers,
            )
        finally:
            self._reads = reads

    @contextmanager
    def dataset_scan_reads(self) -> Iterator[set[str]]:
        """Collect the members one ``scan()`` override reads.

        While active, every FlowView member read — each property, each
        record field — lands in the yielded set. The engine memoizes
        the scan's result only when the set stays inside
        :attr:`DATASET_SCAN_READS`; a read beyond the lawful surface
        means the result is not a pure function of (epoch, body,
        facet), so it is never stored — fresh scan per flow.
        """
        previous = self._reads
        reads: set[str] = set()
        self._reads = reads
        try:
            yield reads
        finally:
            self._reads = previous

    @property
    def request(self) -> str:
        """The flow's stored raw request (request line + headers + body)."""
        self._note("request")
        if self._request is None:
            self._request = self._read(self._flow_dir / "request")
        return self._request

    @property
    def request_method(self) -> str | None:
        """The request line's method token (``GET``), or ``None``."""
        self._note("request_method")
        return self._parse_request().method

    @property
    def request_path(self) -> str | None:
        """The request line's target (path with query), or ``None``."""
        self._note("request_path")
        return self._parse_request().path

    @property
    def request_query(self) -> tuple[tuple[str, str], ...]:
        """Decoded ``(name, value)`` pairs of the request URL's query."""
        self._note("request_query")
        return self._parse_request().query

    @property
    def request_headers(self) -> tuple[tuple[str, str], ...]:
        """``(name, value)`` pairs of the request's header lines, in order."""
        self._note("request_headers")
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
        self._note("response_headers")
        if self._response_headers is None:
            self._response_headers = self._read(self._flow_dir / "response_headers")
        return self._response_headers

    @property
    def body(self) -> str | None:
        """The stored response body, or ``None`` when none was stored.

        Large bodies read and decode in GIL-dropping chunks (the
        shared ``read_chunked`` cadence). The one-shot lenient decode is a single
        state-machine pass over the whole body — the most expensive
        whole-body C call on this path — while the chunked shape pays
        only its final str-join memcpy (~half the cost, measured) as
        the single uninterruptible op, then hands the loop the GIL
        before the next whole-body op can stack behind it.
        """
        self._note("body")
        if self._body is None:
            body_file = self._record.get("body_file")
            if body_file is not None:
                self._body = self._read_chunked(self._data_dir / body_file)
        return self._body

    @property
    def body_bytes(self) -> bytes | None:
        """The stored response body as raw bytes, or ``None``.

        The bytes-exact view of :attr:`body` for checks that must not
        pass through text decoding — content hashing above all.
        """
        self._note("body_bytes")
        if self._body_bytes is None:
            body_file = self._record.get("body_file")
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
        self._note("content_type")
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
        self._note("is_browser")
        return self._record_view.get("browser_id") is not None

    @staticmethod
    def _read(path: Path) -> str:
        """Read a stored flow file, decoded leniently (binary-safe)."""
        return path.read_bytes().decode("utf-8", errors="replace")

    @staticmethod
    def _read_chunked(path: Path) -> str:
        """Read and leniently decode a stored body in GIL-dropping chunks."""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        parts = [decoder.decode(chunk) for chunk in read_chunked(path)]
        parts.append(decoder.decode(b"", final=True))
        body = "".join(parts)
        # The join is the one whole-body copy this path cannot avoid;
        # hand the loop the GIL right after it instead of stacking the
        # next whole-body op behind it inside one GIL hold.
        time.sleep(0)
        return body
