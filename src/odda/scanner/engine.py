# See ADR-0034, ADR-0037.
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import queue
import threading
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from odda.scanner.fastscan import FastScan
from odda.scanner.gates import passes_literal_gates
from odda.scanner.model import Evidence, Finding, FlowView, Rule
from odda.scanner.plumbing import read_chunked, sre_constants, sre_parse

if TYPE_CHECKING:
    import re
    from collections.abc import AbstractSet, Sequence

logger = logging.getLogger(__name__)

_EVIDENCE_DETAIL_MAX = 200

_RESPONSE_BODY = "response_body"
_RESPONSE_HEADERS = "response_headers"
_REQUEST_QUERY = "request_query"
_REQUEST_HEADERS = "request_headers"

_EXPANSION_MAX = 64

_MEMO_BUDGET_BYTES = 8 * 1024 * 1024
_MEMO_MAX_ENTRIES = 1024

_HASH_CHUNK = 1 << 20

_MEMO_MISS = object()


def _node_expansion(op: Any, av: Any) -> int:
    if op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT):
        mn, mx = av[0], av[1]
        count = mx if mx is not sre_constants.MAXREPEAT else mn
        return max(1, count) * max(1, _expansion(av[-1]))
    if op is sre_constants.SUBPATTERN:
        return _expansion(av[-1])
    if op is sre_constants.BRANCH:
        return max((_expansion(d) for d in av[1]), default=0)
    if op in (sre_constants.ASSERT, sre_constants.ASSERT_NOT):
        return _expansion(av[1])
    if op is sre_constants.GROUPREF_EXISTS:
        return max(_expansion(av[1]), _expansion(av[2] or ()))
    return 0


def _expansion(node: Any) -> int:
    return sum(_node_expansion(op, av) for op, av in node)


def _within_expansion_budget(pattern: re.Pattern[str]) -> bool:
    if sre_parse is None:
        return False
    try:
        tree = sre_parse(pattern.pattern, pattern.flags)
    except Exception:
        return False
    return _expansion(tree) <= _EXPANSION_MAX


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
else:
    _WIDE_CATEGORIES = frozenset()


def _affordable(pattern: re.Pattern[str]) -> bool:
    if sre_parse is None:
        return False
    try:
        tree = sre_parse(pattern.pattern, pattern.flags)
    except Exception:
        return False
    return _affordable_tree(tree, under_bound=False, states=1)


def _affordable_tree(tree, *, under_bound: bool, states: int) -> bool:
    return all(
        _affordable_node(node, under_bound=under_bound, states=states) for node in tree
    )


def _affordable_node(node, *, under_bound: bool, states: int) -> bool:
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
    for op, av in items:
        if op is sre_constants.NEGATE:
            return True
        if op is sre_constants.RANGE and av[1] - av[0] > _MAX_RANGE_SPAN:
            return True
        if op is sre_constants.CATEGORY and av in _WIDE_CATEGORIES:
            return True
    return False


def _compile_body_scan(
    patterns: list[re.Pattern[str]],
    shape_screened: Sequence[bool],
) -> tuple[FastScan | None, list[int | None]]:
    db_of: list[int | None] = [None] * len(patterns)
    eligible = [
        (i, pattern)
        for i, pattern in enumerate(patterns)
        if _within_expansion_budget(pattern)
        and (not shape_screened[i] or _affordable(pattern))
    ]
    if not eligible:
        return None, db_of
    try:
        fast = FastScan.build([pattern for _, pattern in eligible])
    except ValueError:
        return None, db_of
    refused = fast.refused
    for slot, (i, _pattern) in enumerate(eligible):
        if slot not in refused:
            db_of[i] = slot
    return fast, db_of


class _RulesetEpoch:
    __slots__ = (
        "corpus_layout",
        "fast",
        "has_scan_overrides",
        "layout",
        "memo",
        "memoizable",
        "rules",
        "scans_body",
        "serial",
    )

    def __init__(self, rules: Sequence[Rule], serial: int = 0) -> None:
        self.rules = tuple(rules)
        self.serial = serial
        self.has_scan_overrides = any(
            type(rule).scan is not Rule.scan for rule in self.rules
        )
        flat: list[re.Pattern[str]] = []
        rows: list[range] = []
        corpus: list[re.Pattern[str]] = []
        corpus_rows: list[range] = []
        for rule in self.rules:
            rows.append(range(len(flat), len(flat) + len(rule.body_patterns)))
            flat.extend(rule.body_patterns)
            corpus_rows.append(
                range(len(corpus), len(corpus) + len(rule.body_corpus()))
            )
            corpus.extend(rule.body_corpus())
        self.fast, db_of = _compile_body_scan(
            [*flat, *corpus], [False] * len(flat) + [True] * len(corpus)
        )
        self.layout = tuple(tuple(db_of[i] for i in row) for row in rows)
        self.corpus_layout = tuple(
            tuple(db_of[len(flat) + i] for i in row) for row in corpus_rows
        )
        self.scans_body = bool(flat) or bool(corpus)
        self.memoizable = self.scans_body or self.has_scan_overrides
        self.memo = _BodyMemo(_MEMO_BUDGET_BYTES, _MEMO_MAX_ENTRIES)


class _BodyStage(NamedTuple):
    error: Exception | None
    body_evidence: tuple[tuple[Evidence, ...], ...]
    corpus_spans: tuple[tuple[Sequence[tuple[int, int]] | None, ...] | None, ...]


class _StageKey(NamedTuple):
    epoch: int
    digest: bytes


class _ScanKey(NamedTuple):
    epoch: int
    digest: bytes
    context: tuple[Any, ...]
    rule: int


class _BodyMemo:
    __slots__ = ("_budget", "_cap", "_entries", "_stored_bytes")

    def __init__(self, budget: int, cap: int) -> None:
        self._budget = budget
        self._cap = cap
        self._entries: OrderedDict[_StageKey | _ScanKey, tuple[Any, int]] = (
            OrderedDict()
        )
        self._stored_bytes = 0

    def get(self, key: _StageKey | _ScanKey) -> Any:
        entry = self._entries.get(key)
        if entry is None:
            return _MEMO_MISS
        self._entries.move_to_end(key)
        return entry[0]

    def put(self, key: _StageKey | _ScanKey, value: Any, size: int) -> None:
        if size > self._budget:
            return
        old = self._entries.pop(key, None)
        if old is not None:
            self._stored_bytes -= old[1]
        self._entries[key] = (value, size)
        self._stored_bytes += size
        while len(self._entries) > self._cap or self._stored_bytes > self._budget:
            _, (_, evicted) = self._entries.popitem(last=False)
            self._stored_bytes -= evicted

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def bytes(self) -> int:
        return self._stored_bytes


def _evidence_entry_bytes(evidence: Evidence) -> int:
    return 64 + len(evidence.where) + len(evidence.detail)


def _stage_size(stage: _BodyStage) -> int:
    return (
        128
        + sum(
            _evidence_entry_bytes(evidence)
            for per_rule in stage.body_evidence
            for evidence in per_rule
        )
        + sum(
            32 * len(spans)
            for per_rule in stage.corpus_spans
            if per_rule is not None
            for spans in per_rule
            if spans is not None
        )
    )


def _findings_size(findings: tuple[Finding, ...]) -> int:
    return 96 * len(findings) + sum(
        64
        + len(finding.rule_id)
        + len(finding.flow_id)
        + len(finding.title)
        + sum(len(ref) for ref in finding.refs)
        + sum(_evidence_entry_bytes(e) for e in finding.evidence)
        for finding in findings
    )


def _rule_body_evidence(
    rule: Rule,
    layout: tuple[int | None, ...],
    text: str | None,
    spans: list[list[tuple[int, int]]] | None,
) -> tuple[Evidence, ...]:
    if not rule.body_patterns or text is None:
        return ()
    evidence: list[Evidence] = []
    for pattern, db_index in zip(rule.body_patterns, layout, strict=True):
        if db_index is None or spans is None:
            evidence += _pattern_evidence((pattern,), text, _RESPONSE_BODY)
        else:
            evidence += _span_evidence(spans[db_index], text)
    return tuple(evidence)


class Scanner:
    def __init__(self, data_dir: Path, rules: Sequence[Rule] = ()) -> None:
        self._data_dir = Path(data_dir)
        self._rules: list[Rule] = list(rules)
        self._epoch_serial = 0
        self._epoch = _RulesetEpoch(self._rules, self._epoch_serial)
        self._build_lock = threading.Lock()
        self._queue: queue.SimpleQueue[tuple[dict[str, Any], _RulesetEpoch]] = (
            queue.SimpleQueue()
        )
        self._wakeup = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._findings_path = self._data_dir / "scanner" / "findings.jsonl"

    def observe(self, record: dict[str, Any]) -> None:
        try:
            self._queue.put((record, self._epoch))
            loop = self._loop
            if loop is not None:
                loop.call_soon_threadsafe(self._wakeup.set)
        except Exception:
            logger.exception("scanner enqueue failed; flow write unaffected")

    def register_rules(self, rules: Sequence[Rule]) -> None:
        with self._build_lock:
            new_ids = {rule.id for rule in rules}
            self._rules = [rule for rule in self._rules if rule.id not in new_ids]
            self._rules.extend(rules)
            self._epoch_serial += 1
            self._epoch = _RulesetEpoch(self._rules, self._epoch_serial)

    def deregister_rules(self, rule_ids: AbstractSet[str]) -> None:
        with self._build_lock:
            self._rules = [rule for rule in self._rules if rule.id not in rule_ids]
            self._epoch_serial += 1
            self._epoch = _RulesetEpoch(self._rules, self._epoch_serial)

    async def run(self) -> None:
        # See ADR-0034.
        self._loop = asyncio.get_running_loop()
        self._wakeup.set()
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            while True:
                try:
                    record, epoch = self._queue.get_nowait()
                except queue.Empty:
                    break
                await asyncio.to_thread(self._scan, record, epoch)

    def _scan(self, record: dict[str, Any], epoch: _RulesetEpoch) -> None:
        flow_id = str(record.get("id", "?"))
        view = FlowView(self._data_dir, record)
        digest = self._body_digest(view) if epoch.memoizable else None
        stage = self._body_stage(view, epoch, digest)
        for index, rule in enumerate(epoch.rules):
            try:
                # See ADR-0035.
                findings = self._scan_rule(rule, view, epoch, digest, index, stage)
                if findings is not None:
                    for finding in findings:
                        self._emit(finding)
                    continue
                evidence = self._match(rule, view, stage, index)
            except Exception:
                logger.exception(
                    "scanner rule %r failed on flow %s; skipped", rule.id, flow_id
                )
                continue
            if evidence:
                self._emit(
                    Finding(
                        rule_id=rule.id,
                        flow_id=flow_id,
                        severity=rule.severity,
                        title=rule.title,
                        evidence=tuple(evidence),
                        refs=rule.refs,
                    )
                )

    def _body_digest(self, view: FlowView) -> bytes | None:
        body_file = view.record.get("body_file")
        if body_file is None:
            return None
        # See ADR-0045.
        digest_hex = view.record.get("body_sha256")
        if digest_hex:
            try:
                return bytes.fromhex(digest_hex)
            except ValueError:
                logger.warning(
                    "flow %s: malformed body_sha256; falling back to re-hash",
                    view.record.get("id"),
                )
        hasher = hashlib.sha256()
        try:
            for chunk in read_chunked(self._data_dir / body_file, _HASH_CHUNK):
                hasher.update(chunk)
        except Exception:
            return None
        return hasher.digest()

    def _body_stage(
        self, view: FlowView, epoch: _RulesetEpoch, digest: bytes | None
    ) -> _BodyStage:
        if not epoch.scans_body:
            return _BodyStage(None, (), (None,) * len(epoch.rules))
        key = _StageKey(epoch.serial, digest) if digest is not None else None
        if key is not None:
            cached = epoch.memo.get(key)
            if cached is not _MEMO_MISS:
                return cached
        try:
            text = view.body
            spans = (
                epoch.fast.find_all(text)
                if text is not None and epoch.fast is not None
                else None
            )
            stage = _BodyStage(
                None,
                tuple(
                    _rule_body_evidence(rule, layout, text, spans)
                    for rule, layout in zip(epoch.rules, epoch.layout, strict=True)
                ),
                tuple(
                    None
                    if not layout
                    else tuple(
                        tuple(spans[db])
                        if db is not None and spans is not None
                        else None
                        for db in layout
                    )
                    for layout in epoch.corpus_layout
                ),
            )
        except Exception as exc:
            return _BodyStage(exc, (), (None,) * len(epoch.rules))
        if key is not None:
            epoch.memo.put(key, stage, _stage_size(stage))
        return stage

    def _scan_rule(
        self,
        rule: Rule,
        view: FlowView,
        epoch: _RulesetEpoch,
        digest: bytes | None,
        index: int,
        stage: _BodyStage,
    ) -> list[Finding] | None:
        body_spans = stage.corpus_spans[index]
        args = (body_spans,) if body_spans is not None else ()
        if digest is None or type(rule).scan is Rule.scan:
            return rule.scan(view, *args)
        key = _ScanKey(epoch.serial, digest, view.dataset_facet(), index)
        cached = epoch.memo.get(key)
        if cached is not _MEMO_MISS:
            flow_id = str(view.record.get("id", "?"))
            return [replace(finding, flow_id=flow_id) for finding in cached]
        with view.dataset_scan_reads() as reads:
            findings = rule.scan(view, *args)
        if findings is not None and reads <= FlowView.DATASET_SCAN_READS:
            frozen = tuple(findings)
            epoch.memo.put(key, frozen, _findings_size(frozen))
        return findings

    def _match(
        self,
        rule: Rule,
        view: FlowView,
        stage: _BodyStage,
        index: int,
    ) -> list[Evidence]:
        browser = view.is_browser
        query_patterns, request_header_patterns = _request_patterns(
            rule, browser=browser
        )
        if not any(
            (
                rule.body_patterns,
                rule.header_patterns,
                query_patterns,
                request_header_patterns,
            )
        ):
            return []
        if not _passes_gates(rule, view.record, view):
            return []
        evidence = _response_evidence(rule, view, stage, index)
        return [
            *evidence,
            *_request_evidence(query_patterns, request_header_patterns, view),
        ]

    def _emit(self, finding: Finding) -> None:
        try:
            record = {
                "rule_id": finding.rule_id,
                "flow_id": finding.flow_id,
                "severity": finding.severity.value,
                "title": finding.title,
                "evidence": [
                    {"where": e.where, "offset": e.offset, "detail": e.detail}
                    for e in finding.evidence
                ],
                "refs": list(finding.refs),
            }
            payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
            # See ADR-0017.
            self._findings_path.parent.mkdir(parents=True, exist_ok=True)
            with self._findings_path.open("ab") as fh:
                fh.write(payload)
        except Exception:
            logger.exception(
                "scanner could not record finding for rule %r; skipped",
                finding.rule_id,
            )


def _content_type_matches(specs: tuple[str, ...], actual: str | None) -> bool:
    mime = (actual or "").split(";", 1)[0].strip().lower()
    return bool(mime) and any(mime.startswith(spec) for spec in specs)


def _pattern_evidence(
    patterns: tuple[re.Pattern[str], ...], text: str, where: str
) -> list[Evidence]:
    evidence: list[Evidence] = []
    for pattern in patterns:
        if not passes_literal_gates(pattern, text):
            continue
        evidence.extend(
            Evidence(
                where=where,
                offset=match.start(),
                detail=match.group(0)[:_EVIDENCE_DETAIL_MAX],
            )
            for match in pattern.finditer(text)
        )
    return evidence


def _span_evidence(spans: Sequence[tuple[int, int]], text: str) -> list[Evidence]:
    return [
        Evidence(
            where=_RESPONSE_BODY,
            offset=start,
            detail=text[start:end][:_EVIDENCE_DETAIL_MAX],
        )
        for start, end in spans
    ]


def _stored_response_headers(view: FlowView) -> str | None:
    try:
        return view.response_headers
    except FileNotFoundError:
        return None


def _passes_gates(rule: Rule, record: dict[str, Any], view: FlowView) -> bool:
    if rule.schemes and record.get("scheme") not in rule.schemes:
        return False
    if rule.statuses and record.get("status_code") not in rule.statuses:
        return False
    if rule.content_types and not _content_type_matches(
        rule.content_types, view.content_type
    ):
        return False
    if rule.browser_only:
        return view.is_browser
    return True


def _request_patterns(
    rule: Rule, *, browser: bool
) -> tuple[tuple[re.Pattern[str], ...], tuple[re.Pattern[str], ...]]:
    if rule.request_browser_only and not browser:
        return (), ()
    return rule.request_query_patterns, rule.request_header_patterns


def _response_evidence(
    rule: Rule,
    view: FlowView,
    stage: _BodyStage,
    index: int,
) -> list[Evidence]:
    evidence: list[Evidence] = []
    if rule.body_patterns:
        if stage.error is not None:
            raise stage.error
        evidence.extend(stage.body_evidence[index])
    if rule.header_patterns:
        headers = _stored_response_headers(view)
        if headers is not None:
            evidence += _pattern_evidence(
                rule.header_patterns, headers, _RESPONSE_HEADERS
            )
    return evidence


def _request_evidence(
    query_patterns: tuple[re.Pattern[str], ...],
    request_header_patterns: tuple[re.Pattern[str], ...],
    view: FlowView,
) -> list[Evidence]:
    evidence: list[Evidence] = []
    if query_patterns:
        evidence += _pair_evidence(query_patterns, _query_pairs(view), _REQUEST_QUERY)
    if request_header_patterns:
        evidence += _pair_evidence(
            request_header_patterns, _request_header_lines(view), _REQUEST_HEADERS
        )
    return evidence


def _query_pairs(view: FlowView) -> list[tuple[str, str]]:
    try:
        pairs = view.request_query
    except FileNotFoundError:
        return []
    return [(name, f"{name}={value}") for name, value in pairs]


def _request_header_lines(view: FlowView) -> list[tuple[str, str]]:
    try:
        headers = view.request_headers
    except FileNotFoundError:
        return []
    return [(f"{name}: {value}", f"{name}: {value}") for name, value in headers]


def _pair_evidence(
    patterns: tuple[re.Pattern[str], ...],
    items: Sequence[tuple[str, str]],
    where: str,
) -> list[Evidence]:
    return [
        Evidence(where=where, offset=index, detail=detail[:_EVIDENCE_DETAIL_MAX])
        for index, (text, detail) in enumerate(items)
        for pattern in patterns
        if pattern.search(text)
    ]
