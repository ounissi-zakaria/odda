"""retire.js dataset adapter: the ``js.outdated-library`` rule slot.

The upstream retire.js vulnerability repository (Apache-2.0, license
vendored alongside) is the whole data source: it rides in this slot
directory as ``jsrepository.json`` and is loaded offline from disk —
no fetch happens at scan time; the vendored snapshot is definitive.
Installing this slot from the file path copies the dataset (and its
license) into the installed slot dir, so the rule stays
self-contained, offline, and editable in place.

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

``filecontent`` patterns do not scan one-by-one: every library's
patterns are compiled into one fast-scan database (the scanner's
multi-pattern seam — one GIL-released engine pass per body answers
them all), built on first use and rebuilt naturally on reinstall
because a reinstall re-executes this slot module. Patterns whose shape
the engine's compiler cannot afford — bounded repeats unroll in it, so
wide atoms or unbounded sub-repeats under them compile for seconds to
minutes — stay on the gated stdlib path, as do patterns the seam
refuses outright (zero-width constructs); patterns the engine only
approximates (backreferences) ride the pass too, confirmed by the
true pattern on every approximation hit (ADR-0037).

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
from pathlib import Path
from typing import Any

from odda.scanner import Evidence, Finding, FlowView, Rule, Severity
from odda.scanner.fastscan import FastScan, sre_constants, sre_parse

# The necessary-literal gate is the scanner's own pre-regex
# optimization (a gate miss skips the linear scan over a multi-MB
# body), and odda.scanner.gates is its public seam: this adapter
# keeps it in front of every stdlib residual the fast-scan pass
# leaves behind (build-refused and over-budget shapes).
from odda.scanner.gates import passes_literal_gates

logger = logging.getLogger(__name__)

# The dataset rides in this slot directory, next to this file
# (Apache-2.0; see jsrepository.LICENSE next to it).
_DATASET_FILENAME = "jsrepository.json"

# The named capture group every dataset pattern's version lands in.
_VERSION_GROUP = "version"

# retire.js's own expansion of the §§version§§ placeholder
# (node/lib/retire.js: replaceVersion) — kept identical so the
# vendored regexes mean what upstream means.
_VERSION_CLASS = r"[0-9][0-9.a-z_\\-]+"

# Evidence "where" values: matches on the flow record's URL path point
# there; body matches point at the stored response body.
_REQUEST_PATH = "request_path"
_RESPONSE_BODY = "response_body"

# Content-type prefixes a flow must carry to be a JS body at all.
_JS_CONTENT_TYPES: tuple[str, ...] = (
    "text/javascript",
    "application/javascript",
    "application/x-javascript",
    "text/jscript",
)

# The dataset's four advisory severities map onto the scanner's scale
# name-for-name (nothing in the dataset maps to info); Severity.rank
# (the scale's own triage order) powers the aggregate "highest
# matched advisory wins" severity.
_SEVERITY_MAP: dict[str, Severity] = {
    "low": Severity.LOW,
    "medium": Severity.MEDIUM,
    "high": Severity.HIGH,
    "critical": Severity.CRITICAL,
}

# The engine's compiler unrolls bounded repeats — one state per step —
# and blows up combinatorially when the unrolled content is wide (a
# negated class, `.`) or itself repeats without a bound. Measured on
# the vendored dataset: jquery `.{50,300}` compiles in 2.3 s, tinyMCE
# `.{0,1200}` in 79 s, vue `[^\n]{0,8000}` past 90 s, while every
# screened-in pattern compiles in under 25 ms. Bounded nesting is
# capped instead of rejected outright (small fixed counts are cheap).
_MAX_NESTED_STATES = 256
_MAX_RANGE_SPAN = 128
if sre_constants is not None:
    _WIDE_CATEGORIES = frozenset(
        getattr(sre_constants, name)
        for name in (
            "CATEGORY_NOT_DIGIT",
            "CATEGORY_NOT_SPACE",
            "CATEGORY_NOT_WORD",
            "CATEGORY_UNI_NOT_DIGIT",
            "CATEGORY_UNI_NOT_SPACE",
            "CATEGORY_UNI_NOT_WORD",
        )
    )
else:  # pragma: no cover — screen off; the stdlib path takes over
    _WIDE_CATEGORIES = frozenset()


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
class _Route:
    """How one filecontent pattern reads off the shared fast-scan pass.

    ``slot`` indexes the corpus pass's per-pattern result list; ``-1``
    marks a pattern that never entered the database (shape the engine
    compiler cannot afford, or a build refusal) and scans stdlib,
    gated. Routed slots' spans are confirmed (ADR-0037): exact and
    approximated patterns alike report the find-iterator's own spans,
    so every match below is real.
    """

    pattern: re.Pattern[str]
    slot: int


@dataclass(frozen=True, slots=True)
class _Corpus:
    """Every library's filecontent patterns behind one fast-scan pass.

    ``entries`` pairs each library (dataset order — the priority the
    adapter has always walked) with its patterns' routes, in the
    per-library order the adapter has always tried them.
    """

    scan: FastScan
    entries: tuple[tuple[_Library, tuple[_Route, ...]], ...]


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

        # One GIL-released engine pass over every library's filecontent
        # patterns answers them all (the hundreds of sequential stdlib
        # scans this replaces were ~92% of scan cost); hash and
        # filename extractors stay as they were (a dict lookup and a
        # search over the flow's short path).
        corpus = _filecontent_corpus()
        results = corpus.scan.find_all(body)

        evidence: list[Evidence] = []
        refs: list[str] = []
        worst = Severity.INFO
        path = str(view.record.get("path") or "")
        for library, routes in corpus.entries:
            detected = _detect(library, routes, path, body, digest, results)
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
    routes: tuple[_Route, ...],
    path: str,
    body: str,
    digest: str,
    results: list[list[tuple[int, int]]],
) -> tuple[str, str, int] | None:
    """Detect a library version from a flow's stored body and path.

    Returns ``(version, extractor, offset)`` — the offset of the
    matching text in the matched material — or ``None`` when no
    extractor speaks. The exact hash wins over the file name, and the
    file name over a content match; ``results`` is the shared
    fast-scan pass over ``body`` that the filecontent routes read.
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
    for route in routes:
        match = _filecontent_match(route, results, body)
        if match is not None:
            version = match.group(_VERSION_GROUP)
            if version:
                return version, "filecontent", match.start()
    return None


def _filecontent_match(
    route: _Route, results: list[list[tuple[int, int]]], body: str
) -> re.Match[str] | None:
    """The leftmost match of one routed filecontent pattern, or ``None``.

    Routed patterns anchor at the pass's first span start — the same
    leftmost-first match ``search`` returns, without rescanning the
    body. The spans are the find-iterator's own for exact and
    approximated slots alike (the seam confirms approximations on
    their hits, ADR-0037), so the first span is a true match start.
    Patterns that never entered the database scan stdlib, gated,
    exactly as every pattern did before the seam.
    """
    if route.slot < 0:
        if not passes_literal_gates(route.pattern, body):
            return None
        return route.pattern.search(body)
    spans = results[route.slot]
    if not spans:
        return None
    return route.pattern.match(body, spans[0][0])


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
    """The compiled dataset, loaded from this slot dir on first use."""
    dataset = Path(__file__).resolve().parent / _DATASET_FILENAME
    document = json.loads(dataset.read_text(encoding="utf-8"))
    return tuple(_compile_library(name, entry) for name, entry in document.items())


@cache
def _filecontent_corpus() -> _Corpus:
    """Compile every library's filecontent patterns into one fast scan.

    Built on first use — the drain's worker thread, where the dataset
    parse it extends already happens — so installing or booting the
    slot stays cheap; a reinstall re-executes this slot module, which
    restarts the caches and rebuilds against the new dataset. The
    build screens every pattern's shape first (see ``_affordable``):
    the screened-out patterns keep their gated stdlib scans.
    """
    libraries = _libraries()
    flattened = [pattern for library in libraries for pattern in library.filecontent]
    screenable = [
        (index, pattern)
        for index, pattern in enumerate(flattened)
        if _affordable(pattern)
    ]
    scan, accepted = _build_filecontent_scan(screenable)
    slot_of = {original: slot for slot, original in enumerate(accepted)}
    entries: list[tuple[_Library, tuple[_Route, ...]]] = []
    position = 0
    for library in libraries:
        routes = []
        for pattern in library.filecontent:
            routes.append(_Route(pattern, slot_of.get(position, -1)))
            position += 1
        entries.append((library, tuple(routes)))
    return _Corpus(scan=scan, entries=tuple(entries))


def _build_filecontent_scan(
    screenable: list[tuple[int, re.Pattern[str]]],
) -> tuple[FastScan, tuple[int, ...]]:
    """Build the shared database over the screened patterns.

    Returns the scan plus the original indices it accepted, in build
    order (result slot ``k`` answers ``screenable[k][1]``). A pattern
    the seam refuses outright — zero-width constructs, which no
    approximation can stand in for — refuses the whole batch build,
    so the fallback triages per pattern and the refusals keep their
    gated stdlib scans instead.
    """
    patterns = tuple(pattern for _, pattern in screenable)
    try:
        return FastScan.build(patterns), tuple(original for original, _ in screenable)
    except ValueError as exc:
        logger.warning(
            "retire.js fast-scan build refused a pattern (%s); triaging per pattern",
            exc,
        )
    keep = {original for original, pattern in screenable if _buildable(pattern)}
    return (
        FastScan.build(
            tuple(pattern for original, pattern in screenable if original in keep)
        ),
        tuple(original for original, _ in screenable if original in keep),
    )


def _buildable(pattern: re.Pattern[str]) -> bool:
    """Whether the seam can compile or approximate one pattern alone."""
    try:
        FastScan.build((pattern,))
    except ValueError:
        return False
    return True


def _affordable(pattern: re.Pattern[str]) -> bool:
    r"""Whether one pattern's shape compiles in the engine cheaply.

    The engine's compiler unrolls bounded repeats into one state per
    step, so a wide atom (``.``, a negated class) or an unbounded
    sub-repeat under one turns the compile from milliseconds into
    seconds or worse — vue's ``[^\n]{0,8000}`` never finished in 90 s.
    Screened-out patterns lose nothing but speed: they fall back to
    the gated stdlib scan this adapter has always had.
    """
    if sre_parse is None:  # pragma: no cover — stdlib path takes over
        return False
    try:
        tree = sre_parse(pattern.pattern, pattern.flags)
    except Exception:
        return False
    return _affordable_tree(tree, under_bound=False, states=1)


def _affordable_tree(tree, *, under_bound: bool, states: int) -> bool:
    """Walk one parsed subpattern; ``False`` marks a shape to screen out."""
    return all(
        _affordable_node(node, under_bound=under_bound, states=states) for node in tree
    )


def _affordable_node(node, *, under_bound: bool, states: int) -> bool:
    """Screen one parse-tree node (and, for containers, its subtree)."""
    op, av = node
    if op in (
        sre_constants.LITERAL,
        sre_constants.AT,
        sre_constants.GROUPREF,
    ):
        return True
    if op in (sre_constants.NOT_LITERAL, sre_constants.ANY):
        return not under_bound
    if op is sre_constants.IN:
        return not under_bound or not _class_is_wide(av)
    if op is sre_constants.SUBPATTERN:
        return _affordable_tree(av[3], under_bound=under_bound, states=states)
    if op is sre_constants.BRANCH:
        return all(
            _affordable_tree(branch, under_bound=under_bound, states=states)
            for branch in av[1]
        )
    if op in (sre_constants.ASSERT, sre_constants.ASSERT_NOT):
        return _affordable_tree(av[1], under_bound=under_bound, states=states)
    if op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT):
        return _affordable_repeat(av, under_bound=under_bound, states=states)
    return False


def _affordable_repeat(av, *, under_bound: bool, states: int) -> bool:
    """Screen one repeat.

    Unrolling is what costs, so its body and bound decide (an
    unbounded repeat loops — but never inside another repeat, where
    the combination simulates).
    """
    _mn, mx, sub = av
    if mx is sre_constants.MAXREPEAT:
        return not under_bound and _affordable_tree(
            sub, under_bound=under_bound, states=states
        )
    unrolled = states * max(mx, 1)
    return _affordable_tree(sub, under_bound=True, states=unrolled) and not (
        under_bound and unrolled > _MAX_NESTED_STATES
    )


def _class_is_wide(items: Any) -> bool:
    """Whether a character class covers too much to unroll cheaply."""
    for op, av in items:
        if op is sre_constants.NEGATE:
            return True
        if op is sre_constants.RANGE and av[1] - av[0] > _MAX_RANGE_SPAN:
            return True
        if op is sre_constants.CATEGORY and av in _WIDE_CATEGORIES:
            return True
    return False


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
