# See ADR-0037.
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from odda.scanner import (
    Evidence,
    Finding,
    FlowView,
    Rule,
    Severity,
    passes_literal_gates,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

_DATASET_FILENAME = "jsrepository.json"

_VERSION_GROUP = "version"

_VERSION_CLASS = r"[0-9][0-9.a-z_\\-]+"

_REQUEST_PATH = "request_path"
_RESPONSE_BODY = "response_body"

_JS_CONTENT_TYPES: tuple[str, ...] = (
    "text/javascript",
    "application/javascript",
    "application/x-javascript",
    "text/jscript",
)

_SEVERITY_MAP: dict[str, Severity] = {
    "low": Severity.LOW,
    "medium": Severity.MEDIUM,
    "high": Severity.HIGH,
    "critical": Severity.CRITICAL,
}


@dataclass(frozen=True, slots=True)
class _Advisory:
    below: str | None
    at_or_above: str | None
    severity: Severity
    summary: str
    refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Library:
    name: str
    filename: tuple[re.Pattern[str], ...]
    filecontent: tuple[re.Pattern[str], ...]
    hashes: dict[str, str]
    advisories: tuple[_Advisory, ...]


@dataclass(frozen=True, slots=True)
class OutdatedLibraryRule(Rule):
    def body_corpus(self) -> tuple[re.Pattern[str], ...]:
        return _filecontent_plan()[0]

    def scan(
        self,
        view: FlowView,
        body_spans: tuple[Sequence[tuple[int, int]] | None, ...] | None = None,
    ) -> list[Finding]:
        mime = (view.content_type or "").split(";", 1)[0].strip().lower()
        if not any(mime.startswith(prefix) for prefix in _JS_CONTENT_TYPES):
            return []
        body = view.body
        body_bytes = view.body_bytes
        if body is None or body_bytes is None:
            return []
        digest = hashlib.sha1(body_bytes).hexdigest()  # noqa: S324

        patterns, rows = _filecontent_plan()
        spans = body_spans if body_spans is not None else (None,) * len(patterns)

        evidence: list[Evidence] = []
        refs: list[str] = []
        worst = Severity.INFO
        path = str(view.record.get("path") or "")
        for library, row in rows:
            routed = [(patterns[i], spans[i]) for i in row]
            detected = _detect(library, routed, path, body, digest)
            if detected is None:
                continue
            version, extractor, offset = detected
            where = _REQUEST_PATH if extractor == "filename" else _RESPONSE_BODY
            for advisory in library.advisories:
                if not _is_vulnerable(version, advisory):
                    continue
                evidence.append(
                    Evidence(
                        where=where,
                        offset=offset,
                        detail=_detail(library.name, version, extractor, advisory),
                    )
                )
                refs.extend(r for r in advisory.refs if r not in refs)
                if advisory.severity.rank > worst.rank:
                    worst = advisory.severity
        if not evidence:
            return []
        return [
            Finding(
                rule_id=self.id,
                flow_id=str(view.record.get("id", "?")),
                severity=worst,
                title=self.title,
                evidence=tuple(evidence),
                refs=tuple(refs),
            )
        ]


RULE = OutdatedLibraryRule(
    id="js.outdated-library",
    severity=Severity.HIGH,
    title="Outdated JavaScript library with known vulnerabilities",
    refs=("https://github.com/RetireJS/retire.js",),
)

RULES = [RULE]


def _detect(
    library: _Library,
    routed: list[tuple[re.Pattern[str], Sequence[tuple[int, int]] | None]],
    path: str,
    body: str,
    digest: str,
) -> tuple[str, str, int] | None:
    version = library.hashes.get(digest)
    if version is not None:
        return version, "sha1", 0
    for pattern in library.filename:
        match = pattern.search(path)
        if match is not None:
            version = match.group(_VERSION_GROUP)
            if version:
                return version, "filename", match.start()
    for pattern, spans in routed:
        match = _filecontent_match(pattern, spans, body)
        if match is not None:
            version = match.group(_VERSION_GROUP)
            if version:
                return version, "filecontent", match.start()
    return None


def _filecontent_match(
    pattern: re.Pattern[str],
    spans: Sequence[tuple[int, int]] | None,
    body: str,
) -> re.Match[str] | None:
    # See ADR-0037.
    if spans is None:
        if not passes_literal_gates(pattern, body):
            return None
        return pattern.search(body)
    if not spans:
        return None
    return pattern.match(body, spans[0][0])


def _detail(name: str, version: str, extractor: str, advisory: _Advisory) -> str:
    label = (
        advisory.summary
        or ", ".join(r for r in advisory.refs if r.startswith("CVE"))
        or "unnamed advisory"
    )
    if advisory.at_or_above is not None:
        bounds = f"vulnerable {advisory.at_or_above} - {advisory.below}"
    else:
        bounds = f"vulnerable below {advisory.below}"
    return (
        f"{name} {version} (via {extractor}) — "
        f"{advisory.severity.value}: {label} ({bounds})"
    )


def _is_vulnerable(version: str, advisory: _Advisory) -> bool:
    if advisory.below is not None and _is_at_or_above(version, advisory.below):
        return False
    floor = advisory.at_or_above
    return floor is None or _is_at_or_above(version, floor)


def _is_at_or_above(version: str, bound: str) -> bool:
    left = [_comparable(c) for c in re.split(r"[.-]", version)]
    right = [_comparable(c) for c in re.split(r"[.-]", bound)]
    width = max(len(left), len(right))
    left += [0] * (width - len(left))
    right += [0] * (width - len(right))
    for a, b in zip(left, right, strict=True):
        a_is_num = isinstance(a, int)
        if a_is_num != isinstance(b, int):
            return a_is_num
        if a > b:  # type: ignore[operator]
            return True
        if a < b:  # type: ignore[operator]
            return False
    return True


def _comparable(component: str) -> int | str:
    return int(component) if component.isdigit() else component


@cache
def _libraries() -> tuple[_Library, ...]:
    dataset = Path(__file__).resolve().parent / _DATASET_FILENAME
    document = json.loads(dataset.read_text(encoding="utf-8"))
    return tuple(_compile_library(name, entry) for name, entry in document.items())


@cache
def _filecontent_plan() -> tuple[
    tuple[re.Pattern[str], ...], tuple[tuple[_Library, range], ...]
]:
    libraries = _libraries()
    patterns = tuple(
        pattern for library in libraries for pattern in library.filecontent
    )
    rows: list[tuple[_Library, range]] = []
    position = 0
    for library in libraries:
        rows.append((library, range(position, position + len(library.filecontent))))
        position += len(library.filecontent)
    return patterns, tuple(rows)


def _compile_library(name: str, entry: dict[str, Any]) -> _Library:
    extractors = entry.get("extractors", {})
    return _Library(
        name=name,
        filename=_compile_patterns(extractors.get("filename", ())),
        filecontent=_compile_patterns(extractors.get("filecontent", ())),
        hashes=dict(extractors.get("hashes", {})),
        advisories=tuple(
            _compile_advisory(raw) for raw in entry.get("vulnerabilities", ())
        ),
    )


def _compile_patterns(raws: Any) -> tuple[re.Pattern[str], ...]:
    compiled: list[re.Pattern[str]] = []
    for raw in raws:
        try:
            pattern = raw.replace(
                "§§version§§", f"(?P<{_VERSION_GROUP}>{_VERSION_CLASS})"
            )
            compiled.append(re.compile(pattern))
        except re.error:
            logger.warning(
                "retire.js pattern for %r not compilable in Python re; skipped",
                raw,
            )
    return tuple(compiled)


def _compile_advisory(raw: dict[str, Any]) -> _Advisory:
    identifiers = raw.get("identifiers") or {}
    cves = identifiers.get("CVE") or ()
    return _Advisory(
        below=raw.get("below"),
        at_or_above=raw.get("atOrAbove"),
        severity=_SEVERITY_MAP[str(raw["severity"])],
        summary=str(identifiers.get("summary", "")),
        refs=(*cves, *(raw.get("info") or ())),
    )
