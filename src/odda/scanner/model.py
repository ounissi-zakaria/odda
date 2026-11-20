# See ADR-0035.
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
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {severity: rank for rank, severity in enumerate(Severity)}


@dataclass(frozen=True, slots=True)
class Evidence:
    where: str
    offset: int
    detail: str


@dataclass(frozen=True, slots=True)
class Finding:
    rule_id: str
    flow_id: str
    severity: Severity
    title: str
    evidence: tuple[Evidence, ...]
    refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Rule:
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
        object.__setattr__(self, "body_patterns", _compile(self.body_patterns))
        object.__setattr__(self, "header_patterns", _compile(self.header_patterns))
        object.__setattr__(
            self, "request_query_patterns", _compile(self.request_query_patterns)
        )
        object.__setattr__(
            self, "request_header_patterns", _compile(self.request_header_patterns)
        )

    def body_corpus(self) -> tuple[re.Pattern[str], ...]:
        return ()

    def scan(
        self,
        view: FlowView,  # noqa: ARG002
        body_spans: tuple[Sequence[tuple[int, int]] | None, ...] | None = None,  # noqa: ARG002
    ) -> list[Finding] | None:
        return None


def _compile(
    patterns: tuple[re.Pattern[str] | str, ...],
) -> tuple[re.Pattern[str], ...]:
    return tuple(
        p if isinstance(p, re.Pattern) else re.compile(p, re.MULTILINE)
        for p in patterns
    )


class _ParsedRequest(NamedTuple):
    method: str | None
    path: str | None
    query: tuple[tuple[str, str], ...]
    headers: tuple[tuple[str, str], ...]


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
    __slots__ = ("_view",)

    def __init__(self, record: dict[str, Any], view: FlowView) -> None:
        super().__init__(record)
        self._view = view

    def __getitem__(self, key: Any) -> Any:
        self._view._note(f"record.{key}")  # noqa: SLF001 — the facet's own log
        return dict.__getitem__(self, key)

    def get(self, key: Any, default: Any = None) -> Any:
        self._view._note(f"record.{key}")  # noqa: SLF001 — the facet's own log
        return dict.get(self, key, default)


class FlowView:
    DATASET_SCAN_READS: ClassVar[frozenset[str]] = frozenset(
        {"body", "body_bytes", "content_type", "response_headers", "is_browser"}
        | {f"record.{field}" for field in _DATASET_RECORD_FIELDS}
    )

    def __init__(self, data_dir: Path, record: dict[str, Any]) -> None:
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
        return self._record_view

    def _note(self, member: str) -> None:
        if self._reads is not None:
            self._reads.add(member)

    def dataset_facet(self) -> tuple[Any, ...]:
        reads = self._reads
        self._reads = None
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
        previous = self._reads
        reads: set[str] = set()
        self._reads = reads
        try:
            yield reads
        finally:
            self._reads = previous

    @property
    def request(self) -> str:
        self._note("request")
        if self._request is None:
            self._request = self._read(self._flow_dir / "request")
        return self._request

    @property
    def request_method(self) -> str | None:
        self._note("request_method")
        return self._parse_request().method

    @property
    def request_path(self) -> str | None:
        self._note("request_path")
        return self._parse_request().path

    @property
    def request_query(self) -> tuple[tuple[str, str], ...]:
        self._note("request_query")
        return self._parse_request().query

    @property
    def request_headers(self) -> tuple[tuple[str, str], ...]:
        self._note("request_headers")
        return self._parse_request().headers

    def _parse_request(self) -> _ParsedRequest:
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
        self._note("response_headers")
        if self._response_headers is None:
            self._response_headers = self._read(self._flow_dir / "response_headers")
        return self._response_headers

    @property
    def body(self) -> str | None:
        self._note("body")
        if self._body is None:
            body_file = self._record.get("body_file")
            if body_file is not None:
                self._body = self._read_chunked(self._data_dir / body_file)
        return self._body

    @property
    def body_bytes(self) -> bytes | None:
        self._note("body_bytes")
        if self._body_bytes is None:
            body_file = self._record.get("body_file")
            if body_file is not None:
                self._body_bytes = (self._data_dir / body_file).read_bytes()
        return self._body_bytes

    @property
    def content_type(self) -> str | None:
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
        self._note("is_browser")
        return self._record_view.get("browser_id") is not None

    @property
    def data_dir(self) -> Path:
        self._note("data_dir")
        return self._data_dir

    @staticmethod
    def _read(path: Path) -> str:
        return path.read_bytes().decode("utf-8", errors="replace")

    @staticmethod
    def _read_chunked(path: Path) -> str:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        parts = [decoder.decode(chunk) for chunk in read_chunked(path)]
        parts.append(decoder.decode(b"", final=True))
        body = "".join(parts)
        time.sleep(0)
        return body
