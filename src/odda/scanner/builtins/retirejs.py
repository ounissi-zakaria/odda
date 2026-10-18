"""retire.js dataset adapter: the ``js.outdated-library`` builtin (ADR-0035).

The upstream retire.js vulnerability repository (Apache-2.0, license
vendored alongside) is the whole data source: it ships as package data
under ``odda/scanner/data/`` and is loaded offline through
``importlib.resources`` — the userscripts/default asset precedent. No
fetch happens at scan time; the vendored snapshot is definitive.

Three of retire.js's four extractor kinds run over the flow store.
``filecontent`` regexes match the stored response body, ``hashes`` map
the SHA-1 of the stored body bytes to an exact version, and
``filename`` regexes match the flow record's path — bodies are stored
as ``response_body<ext>``, so the URL filename only lives there. The
fourth kind, ``func`` (live JS property probes), is a browser-extension
concept with no offline equivalent and is ignored. Priority when more
than one extractor could speak: the exact hash, then the file name,
then a content match. ``§§version§§`` in a dataset regex is retire.js's
version placeholder; this adapter expands it exactly as upstream's
replaceVersion does (digits, then dots, lowercase letters, underscores,
hyphens — as a named capture group).

A version matches an advisory exactly as upstream retire.js decides it:
vulnerable iff strictly below ``below`` (when present) and at-or-above
``atOrAbove`` (when present) — a hand-rolled, stdlib-only compare where
dot/hyphen components compare as ints when numeric, missing components
count as zero, and a numeric component sorts above a string one.

Per flow the adapter emits at most ONE finding (the Finding identity is
rule-flow): severity is the HIGHEST matched advisory's — the per-
advisory mapping the PRD asks for, aggregated — and the evidence
enumerates every matched advisory with its own severity, library,
detected version, and extractor; refs collect each advisory's CVE
identifiers and info URLs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

from odda.scanner.engine import _RESPONSE_BODY
from odda.scanner.model import (
    Evidence,
    Finding,
    FlowView,
    Rule,
    Severity,
    _passes_literal_gates,
)

logger = logging.getLogger(__name__)

# Package location of the vendored dataset (Apache-2.0; see
# jsrepository.LICENSE next to it).
_DATASET_PACKAGE = "odda.scanner.data"
_DATASET_FILENAME = "jsrepository.json"

# The named capture group every dataset pattern's version lands in.
_VERSION_GROUP = "version"

# retire.js's own expansion of the §§version§§ placeholder
# (node/lib/retire.js: replaceVersion) — kept identical so the
# vendored regexes mean what upstream means.
_VERSION_CLASS = r"[0-9][0-9.a-z_\\-]+"

# Evidence "where" value for matches on the flow record's URL path —
# the body "where" comes from the engine, which defines it.
_REQUEST_PATH = "request_path"

# Content-type prefixes a flow must carry to be a JS body at all.
_JS_CONTENT_TYPES: tuple[str, ...] = (
    "text/javascript",
    "application/javascript",
    "application/x-javascript",
    "text/jscript",
)

# The dataset's four advisory severities map onto the scanner's scale
# name-for-name (nothing in the dataset maps to info); order powers the
# aggregate "highest matched advisory wins" severity.
_SEVERITY_MAP: dict[str, Severity] = {
    "low": Severity.LOW,
    "medium": Severity.MEDIUM,
    "high": Severity.HIGH,
    "critical": Severity.CRITICAL,
}
_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


@dataclass(frozen=True, slots=True)
class _Advisory:
    """One dataset vulnerability entry, bounds pre-parsed."""

    below: str | None
    at_or_above: str | None
    severity: Severity
    summary: str
    refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Library:
    """One dataset library entry with its compiled extractors."""

    name: str
    filename: tuple[re.Pattern[str], ...]
    filecontent: tuple[re.Pattern[str], ...]
    hashes: dict[str, str]
    advisories: tuple[_Advisory, ...]


@dataclass(frozen=True, slots=True)
class OutdatedLibraryRule(Rule):
    """``js.outdated-library``: vendored retire.js data behind the Rule seam.

    A dataset-backed rule scans each flow itself (the engine calls
    ``scan`` when a rule has one) because the declarative spec cannot
    express the dataset's extractors or per-advisory severities. The
    inherited ``severity`` is the static listing level; findings carry
    the highest matched advisory's severity instead.
    """

    def scan(self, view: FlowView) -> list[Finding]:
        """Scan one flow view; at most one finding, however many libraries."""
        mime = (view.content_type or "").split(";", 1)[0].strip().lower()
        if not any(mime.startswith(prefix) for prefix in _JS_CONTENT_TYPES):
            return []
        body = view.body
        body_bytes = view.body_bytes
        if body is None or body_bytes is None:
            return []
        # SHA-1 here is the dataset's content-addressing key (a lookup
        # into vendored hashes), not a security primitive.
        digest = hashlib.sha1(body_bytes).hexdigest()  # noqa: S324

        evidence: list[Evidence] = []
        refs: list[str] = []
        worst = Severity.INFO
        path = str(view.record.get("path") or "")
        for library in _libraries():
            detected = _detect(library, path, body, digest)
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
                if _SEVERITY_ORDER[advisory.severity] > _SEVERITY_ORDER[worst]:
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


def _detect(
    library: _Library, path: str, body: str, digest: str
) -> tuple[str, str, int] | None:
    """Detect a library version from a flow's stored body and path.

    Returns ``(version, extractor, offset)`` — the offset of the
    matching text in the matched material — or ``None`` when no
    extractor speaks. The exact hash wins over the file name, and the
    file name over a content match.
    """
    version = library.hashes.get(digest)
    if version is not None:
        return version, "sha1", 0
    for pattern in library.filename:
        match = pattern.search(path)
        if match is not None:
            version = match.group(_VERSION_GROUP)
            if version:
                return version, "filename", match.start()
    for pattern in library.filecontent:
        # Necessary-literal gate first (ticket 08): a miss skips the
        # linear scan — the dominant multi-MB-body cost. The hash and
        # filename extractors above stay as they are (dict lookup and
        # a search over the flow's short path).
        if not _passes_literal_gates(pattern, body):
            continue
        match = pattern.search(body)
        if match is not None:
            version = match.group(_VERSION_GROUP)
            if version:
                return version, "filecontent", match.start()
    return None


def _detail(name: str, version: str, extractor: str, advisory: _Advisory) -> str:
    """One evidence line: library, version, extractor, advisory, severity."""
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
    """Apply upstream's bounds: strictly below, at-or-above the floor."""
    if advisory.below is not None and _is_at_or_above(version, advisory.below):
        return False
    floor = advisory.at_or_above
    return floor is None or _is_at_or_above(version, floor)


def _is_at_or_above(version: str, bound: str) -> bool:
    """retire.js's loose version compare, ported (stdlib only).

    Components split on ``.`` and ``-``; numeric components compare as
    ints, missing components count as 0, and when the kinds differ a
    number sorts above a string — upstream ``toComparable`` semantics.
    """
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
    """One split component: int when fully numeric, else the string."""
    return int(component) if component.isdigit() else component


@cache
def _libraries() -> tuple[_Library, ...]:
    """The compiled dataset, loaded from package data on first use."""
    text = (
        resources.files(_DATASET_PACKAGE)
        .joinpath(_DATASET_FILENAME)
        .read_text(encoding="utf-8")
    )
    document = json.loads(text)
    return tuple(_compile_library(name, entry) for name, entry in document.items())


def _compile_library(name: str, entry: dict[str, Any]) -> _Library:
    """Compile one library entry: patterns, hashes, pre-parsed advisories."""
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
    """Compile dataset regexes; JS-only constructs are skipped, logged.

    One vendored pattern (a lodash variable-width lookbehind) is not
    expressible in Python's ``re``; the library keeps its other
    extractors and the skip is visible in the log once per process.
    """
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
    """Compile one vulnerability entry into an advisory."""
    identifiers = raw.get("identifiers") or {}
    cves = identifiers.get("CVE") or ()
    return _Advisory(
        below=raw.get("below"),
        at_or_above=raw.get("atOrAbove"),
        severity=_SEVERITY_MAP[str(raw["severity"])],
        summary=str(identifiers.get("summary", "")),
        refs=(*cves, *(raw.get("info") or ())),
    )
