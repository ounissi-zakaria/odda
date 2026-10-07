"""Stable rule-author surface for the passive scanner (ADR-0035).

Rules are typed Python registrations, not a DSL: builtins are literals
under :mod:`odda.scanner.builtins`, and user rule files import these
types and expose a module-level ``RULES`` list. This module is the
contract rule authors build against — the surface stays additive across
odda upgrades (new optional fields, never renames).

The match spec is declarative: regex patterns matched against the
flow's stored material, with gates that restrict which flows a rule
sees. A correlator seam exists for future cross-flow analysis; none
ship.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol
from urllib.parse import parse_qsl, urlsplit

if TYPE_CHECKING:
    from pathlib import Path


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
        """The stored response body, or ``None`` when none was stored."""
        if self._body is None:
            body_file = self.record.get("body_file")
            if body_file is not None:
                self._body = self._read(self._data_dir / body_file)
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
