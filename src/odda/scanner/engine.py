"""Scanner engine: enqueue-only observation, async drain, findings index.

ADR-0034's observation model: the flow record writer fires
:meth:`Scanner.observe` after the flows.jsonl line lands, on the
writer's own thread — which is the mitmproxy addon thread for browser
capture and the event-loop thread for ``request_send``. The hook only
enqueues (a thread-safe :class:`queue.SimpleQueue` plus a
``call_soon_threadsafe`` wakeup; ``asyncio.Queue`` is not thread-safe).
The drain coroutine the MCP lifespan owns scans queued records and
appends findings to ``.odda/scanner/findings.jsonl`` (created lazily on
the first finding, like every other data-dir subtree).

Containment is the other contract: every failure — enqueue, one rule,
one finding write — is logged and skipped. A broken rule never blocks
the flow's other rules, and nothing the scanner does can touch the
flow write path that triggered it.

The response-body stage drains through the compiled engine (tickets
12 and 13): every declarative body pattern of the live ruleset builds
into one fast-scan database per ruleset epoch, and each stored body is
scanned once, in a GIL-released pass, whatever the rule count. Gates
(schemes, statuses, content types, browser attribution) filter after
the pass, per rule, before evidence assembly. Patterns the engine can
only approximate — backreferences, lookarounds — ride the same
database as prefilters and confirm on their approximation hits
(ADR-0037); the stdlib residuals are the over-budget shapes and the
build-refused zero-width patterns. Request-query, request-header, and
response-header stages never left the stdlib path (tiny texts,
necessary-literal gated).

Repeated bodies are memoized (ticket 14): the epoch's body-stage
results — every rule's pre-gate body evidence, dataset-backed
``scan()`` results included — are a pure function of (ruleset epoch,
stored body bytes), cached per session under a byte-budget LRU with a
hard entry cap. Gates are flow context and filter per flow after the
memo; evidence assembly beyond the body and Finding emission stay per
flow, so memoization is invisible to everything except drain time.
"""

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

from odda.scanner.fastscan import FastScan, read_chunked, sre_constants, sre_parse
from odda.scanner.gates import passes_literal_gates
from odda.scanner.model import Evidence, Finding, FlowView, Rule

if TYPE_CHECKING:
    import re
    from collections.abc import AbstractSet, Sequence

logger = logging.getLogger(__name__)

# Matched text kept per evidence entry — enough to identify and grep,
# bounded so a pathological body cannot bloat the findings line.
_EVIDENCE_DETAIL_MAX = 200

# Evidence "where" for matches in the stored response body.
_RESPONSE_BODY = "response_body"
# Evidence "where" for matches in the stored response headers.
_RESPONSE_HEADERS = "response_headers"
# Evidence "where" for matches on the request URL's query parameters.
_REQUEST_QUERY = "request_query"
# Evidence "where" for matches on the stored request headers.
_REQUEST_HEADERS = "request_headers"

# Compile-expansion budget for the engine path (ticket 12). Vectorscan
# compiles a bounded repeat by expanding it, with no cost bound the
# engine exposes: the corpus's ``{0,600}?``-class patterns (Telerik
# takeover shapes) compile for minutes — measured, the call never
# returns. The estimate below walks the stdlib parse tree (repeats
# multiply, concatenation adds, literals are free) and keeps
# over-budget patterns on the stdlib path, where the necessary-literal
# gates hold the line. The estimate only ever picks a path — findings
# are byte-identical either way.
_EXPANSION_MAX = 64

# Body memoization (ticket 14): repeated identical bodies stop
# rescanning within a session. The epoch's body-stage results are a
# pure function of (ruleset epoch, stored body bytes), cached under a
# byte-budget LRU with a hard entry cap. Both ceilings are on the
# *stored results* — a match-heavy body stores thousands of evidence
# entries, and a burst of unique bodies must never grow the scanner
# without bound.
_MEMO_BUDGET_BYTES = 8 * 1024 * 1024
_MEMO_MAX_ENTRIES = 1024

# Digest of a stored body: SHA-256 over the file's bytes, read in
# GIL-dropping chunks (hashlib releases the GIL per large update; one
# read_bytes of a multi-MB body would be an uninterruptible C call).
# SHA-256, not a blake2: this is a content key, not a security
# primitive, and SHA-256 rides the hardware extensions (~4x the
# software blake2b here) — the digest is the duplicate's whole cost.
_HASH_CHUNK = 1 << 20

# Memo lookup miss sentinel — never a legitimate cached value.
_MEMO_MISS = object()


def _node_expansion(op: Any, av: Any) -> int:
    """Expansion estimate of one parse node (see ``_EXPANSION_MAX``)."""
    if op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT):
        mn, mx = av[0], av[1]
        # An unbounded tail is a kleene star: only the required count
        # expands. A bounded one expands to its max.
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
    # Literals, classes, anchors, and backrefs add no expansion.
    return 0


def _expansion(node: Any) -> int:
    """Expansion estimate of one parsed concatenation."""
    return sum(_node_expansion(op, av) for op, av in node)


def _within_expansion_budget(pattern: re.Pattern[str]) -> bool:
    """Whether a pattern's bounded-repeat expansion fits the budget.

    Parse failures stay off the engine too — a pattern the guard
    cannot reason about must never reach a compile that might not
    return.
    """
    if sre_parse is None:  # pragma: no cover — stdlib parser gone
        return False
    try:
        tree = sre_parse(pattern.pattern, pattern.flags)
    except Exception:
        return False
    return _expansion(tree) <= _EXPANSION_MAX


def _compile_body_scan(
    patterns: list[re.Pattern[str]],
) -> tuple[FastScan | None, list[int | None]]:
    """Compile the epoch's body patterns into one fast-scan database.

    Returns the database (``None`` when nothing rides it) and, per
    input pattern, its index into the database — or ``None`` for the
    stdlib residuals: over-budget patterns (``_EXPANSION_MAX``) and
    patterns that refuse even approximation (zero-width constructs
    whose ``FastScan.build`` raises). Patterns the engine can only
    approximate ride too (ticket 13): their spans come back confirmed
    — approximation prefilter, stdlib confirm on the hit — so they are
    indistinguishable from exact slots in the layout. The prefilter
    pass costs one whole-body encode plus its engine scan; it is
    load-bearing as the confirm gate, and an epoch with no approximated
    patterns builds no prefilter database at all.
    """
    db_of: list[int | None] = [None] * len(patterns)
    eligible = [
        (i, pattern)
        for i, pattern in enumerate(patterns)
        if _within_expansion_budget(pattern)
    ]
    if not eligible:
        return None, db_of
    try:
        fast = FastScan.build([pattern for _, pattern in eligible])
    except Exception:
        # An eligible pattern refused even approximation (zero-width
        # constructs raise) or the combined compile failed; partition
        # by probe so only the offenders fall back. A pattern must
        # never break the epoch — the worst case is the stdlib path.
        return _partition_body_scan(eligible, len(patterns))
    for slot, (i, _pattern) in enumerate(eligible):
        db_of[i] = slot
    return fast, db_of


def _partition_body_scan(
    eligible: Sequence[tuple[int, re.Pattern[str]]], total: int
) -> tuple[FastScan | None, list[int | None]]:
    """The fallback compile: probe per pattern, build the survivors."""
    db_of: list[int | None] = [None] * total
    keep: list[re.Pattern[str]] = []
    for i, pattern in eligible:
        try:
            FastScan.build((pattern,))
        except Exception:  # noqa: S112 — refusal is the expected signal
            continue  # refused even alone: stdlib residual
        db_of[i] = len(keep)
        keep.append(pattern)
    if not keep:
        return None, db_of
    try:
        fast = FastScan.build(keep)
    except Exception:
        logger.exception(
            "scanner body database compile failed;"
            " the epoch scans its body stage on the stdlib path"
        )
        return None, [None] * total
    return fast, db_of


class _RulesetEpoch:
    """One immutable generation of the live ruleset — the scan unit.

    ``observe`` snapshots the whole object at a flow's arrival, so a
    flow is scanned by exactly the rules (and the compiled body
    database) of the epoch it arrived under: a mid-session install,
    remove, or force-swap builds the next epoch and never applies
    retroactively. ``layout`` maps each rule's body patterns to their
    database index — exact and approximated slots alike, their spans
    arrive confirmed — or ``None`` for the stdlib residuals
    (``_compile_body_scan``). ``serial`` is the generation number the
    body memo keys on (ticket 14): it rises with every mutation, so a
    rule installed after a body was seen cannot be answered by that
    body's cached results — the re-arrival misses the memo and scans
    fresh, exactly as arrival-time install semantics demand.
    """

    __slots__ = (
        "fast",
        "has_scan_overrides",
        "layout",
        "memoizable",
        "rules",
        "scans_body",
        "serial",
    )

    def __init__(self, rules: Sequence[Rule], serial: int = 0) -> None:
        """Freeze the rules tuple and compile its body database."""
        self.rules = tuple(rules)
        self.serial = serial
        # Dataset rules (scan overrides) run their own body work; the
        # memo covers them too, so they count toward digest worth.
        self.has_scan_overrides = any(
            type(rule).scan is not Rule.scan for rule in self.rules
        )
        flat: list[re.Pattern[str]] = []
        rows: list[range] = []
        for rule in self.rules:
            rows.append(range(len(flat), len(flat) + len(rule.body_patterns)))
            flat.extend(rule.body_patterns)
        self.fast, db_of = _compile_body_scan(flat)
        self.layout = tuple(tuple(db_of[i] for i in row) for row in rows)
        self.scans_body = bool(flat)
        # Worth reading a body for its digest at all: any declarative
        # body pattern, or any dataset rule whose scan() is memoized.
        self.memoizable = self.scans_body or self.has_scan_overrides


class _BodyStage(NamedTuple):
    """One epoch's body-stage results for one body, shared by every rule.

    ``body_evidence`` holds each rule's pre-gate body-stage result —
    evidence in the rule's body_patterns order — aligned with the
    epoch's rules. Gates (statuses, content types, schemes, browser
    attribution) are flow context, so they filter per flow after the
    memo and the cached value serves every flow carrying the same
    bytes byte-identically (ticket 14). ``error`` re-raises per rule
    at the point the all-stdlib path raised it, so an unreadable body
    still contained-skips exactly the rules it always did.
    """

    error: Exception | None
    body_evidence: tuple[tuple[Evidence, ...], ...]


class _StageKey(NamedTuple):
    """Memo key of one epoch's body-stage results (ticket 14).

    Fields:
        epoch: The ruleset epoch serial — every mutation clears the
            memo wholesale, so a cached stage can never answer for a
            ruleset generation that no longer exists.
        digest: SHA-256 of the stored body bytes — the body half of
            the key (results are pure in exactly these two).
    """

    epoch: int
    digest: bytes


class _ScanKey(NamedTuple):
    """Memo key of one dataset-scan memo entry (ticket 14).

    Fields:
        epoch: The ruleset epoch serial (see ``_StageKey``).
        digest: SHA-256 of the stored body bytes.
        context: The flow-context slice (``_scan_context``) — every
            record field and stored header a ``scan()`` override
            lawfully reads, so gated-in and gated-out flows of one
            body never share a cached scan.
        rule: The rule's index into the epoch's rules tuple.
    """

    epoch: int
    digest: bytes
    context: tuple[Any, ...]
    rule: int


class _BodyMemo:
    """Session-scoped memo of body-stage results (ticket 14).

    One byte-budget LRU with a hard entry cap: ``put`` inserts at the
    MRU end and evicts from the LRU end until both ceilings hold; a
    single result larger than the whole budget is never stored (it
    could not fit and would evict everything else trying). Reads and
    writes come from the drain worker (one flow at a time through
    ``Scanner._scan``) while ``clear`` comes from the event-loop
    thread's register/deregister — a tiny lock holds that overlap;
    every other operation runs uncontended.
    """

    __slots__ = ("_budget", "_cap", "_entries", "_lock", "_stored_bytes")

    def __init__(self, budget: int, cap: int) -> None:
        """Bound the memo by stored bytes and entry count."""
        self._budget = budget
        self._cap = cap
        self._entries: OrderedDict[_StageKey | _ScanKey, tuple[Any, int]] = (
            OrderedDict()
        )
        self._stored_bytes = 0
        self._lock = threading.Lock()

    def get(self, key: _StageKey | _ScanKey) -> Any:
        """The cached value, promoted to MRU — or ``_MEMO_MISS``."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return _MEMO_MISS
            self._entries.move_to_end(key)
            return entry[0]

    def put(self, key: _StageKey | _ScanKey, value: Any, size: int) -> None:
        """Store ``value`` (``size`` estimated bytes) under ``key``."""
        if size > self._budget:
            return
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._stored_bytes -= old[1]
            self._entries[key] = (value, size)
            self._stored_bytes += size
            while len(self._entries) > self._cap or self._stored_bytes > self._budget:
                _, (_, evicted) = self._entries.popitem(last=False)
                self._stored_bytes -= evicted

    def clear(self) -> None:
        """Drop every entry — a ruleset mutation invalidates all keys."""
        with self._lock:
            self._entries.clear()
            self._stored_bytes = 0

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def bytes(self) -> int:
        """Estimated bytes of stored results — the budget's ledger."""
        return self._stored_bytes


def _evidence_entry_bytes(evidence: Evidence) -> int:
    """Estimated stored bytes of one evidence entry."""
    return 64 + len(evidence.where) + len(evidence.detail)


def _stage_size(stage: _BodyStage) -> int:
    """Estimated stored bytes of one stage entry."""
    return 128 + sum(
        _evidence_entry_bytes(evidence)
        for per_rule in stage.body_evidence
        for evidence in per_rule
    )


def _findings_size(findings: tuple[Finding, ...]) -> int:
    """Estimated stored bytes of one dataset-scan memo entry."""
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
    """One rule's pre-gate body-stage result: engine spans in, evidence out.

    Compiled patterns read the shared pass's spans; the residuals
    (engine-refused or over-expansion-budget patterns) run the gated
    stdlib path — interleaved in the rule's body_patterns order so
    records stay byte-identical to the all-stdlib scan. Pure in
    (epoch, body bytes): this is what the memo stores (ticket 14).
    """
    if not rule.body_patterns or text is None:
        return ()
    evidence: list[Evidence] = []
    for pattern, db_index in zip(rule.body_patterns, layout, strict=True):
        if db_index is None or spans is None:
            evidence += _pattern_evidence((pattern,), text, _RESPONSE_BODY)
        else:
            evidence += _span_evidence(spans[db_index], text)
    return tuple(evidence)


def _scan_context(view: FlowView) -> tuple[Any, ...]:
    """The flow-context slice of the dataset-scan memo key (ticket 14).

    A ``scan()`` override is an opaque body of work, so its memo entry
    keys on the whole FlowView surface such an override lawfully reads
    per flow: the record's stable routing fields (method, scheme,
    host, port, path, browser attribution, status, error) and the
    stored response headers — the response content type above all,
    retire.js's own screen. The stored body is the digest half of the
    key; the flow id is re-stamped per flow (``Scanner._scan_rule``);
    request-side material belongs to the request-stage patterns, not
    to scan() overrides. Same body, different context: a different
    key and a fresh scan, so gated-in and gated-out flows of one body
    never share a cached scan result.
    """
    record = view.record
    # body_file embeds the flow id; only its extension — the stored
    # body's content addressing — is rule-visible context.
    body_file = str(record.get("body_file") or "")
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
        _stored_response_headers(view),
    )


class Scanner:
    """Session-live passive scanner.

    ``observe`` is the writer's post-append hook (enqueue only);
    ``run`` is the lifespan-owned drain task. Findings append to
    ``<data_dir>/scanner/findings.jsonl``, one line per (rule, flow),
    evidence aggregated. Zero egress by construction: the scanner only
    reads captured material and writes the findings index.
    """

    def __init__(self, data_dir: Path, rules: Sequence[Rule] = ()) -> None:
        """Initialize with the session's data dir and the ruleset.

        Args:
            data_dir: The session's ``.odda`` directory; the scanner's
                subtree (``scanner/findings.jsonl``) appears lazily
                under it on the first finding.
            rules: Rules every observed flow is checked against.
        """
        self._data_dir = Path(data_dir)
        # Mutable live set: register/deregister (user-rule installs and
        # removes, ticket #06) run on the event-loop thread; the drain
        # only ever iterates an arrival-time snapshot (see observe), so
        # the list is never read mid-mutation.
        self._rules: list[Rule] = list(rules)
        # The live epoch: the rules tuple plus the compiled body
        # database those rules scan through. register/deregister
        # (boot, install, remove, force-swap) builds the next one on
        # this thread; observe snapshots the object itself.
        self._epoch_serial = 0
        self._epoch = _RulesetEpoch(self._rules, self._epoch_serial)
        # Session-scoped memo of body-stage results (ticket 14): keys
        # carry the epoch serial, so every ruleset mutation clears it
        # wholesale — cached results can never answer for a ruleset
        # generation that no longer exists.
        self._memo = _BodyMemo(_MEMO_BUDGET_BYTES, _MEMO_MAX_ENTRIES)
        self._queue: queue.SimpleQueue[tuple[dict[str, Any], _RulesetEpoch]] = (
            queue.SimpleQueue()
        )
        self._wakeup = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._findings_path = self._data_dir / "scanner" / "findings.jsonl"

    def observe(self, record: dict[str, Any]) -> None:
        """Post-append hook target: enqueue the record and wake the drain.

        Called from the writer's thread (mitmproxy addon thread or the
        event-loop thread). The entry carries a snapshot of the live
        rules taken now, at the flow's arrival: a rule registered after
        this moment (a mid-session install) never matches this flow,
        and one deregistered after it (a remove) still does — install
        semantics key on arrival, not on when the drain happens to run.
        Never raises into the write path: failures are logged and the
        record is simply lost to scanning.
        """
        try:
            # The epoch object is the snapshot: the rules tuple and
            # the body database they compile into, swapped wholesale
            # by register/deregister, so an entry can never mix a
            # rules list with a later epoch's database.
            self._queue.put((record, self._epoch))
            loop = self._loop
            if loop is not None:
                loop.call_soon_threadsafe(self._wakeup.set)
        except Exception:
            logger.exception("scanner enqueue failed; flow write unaffected")

    def register_rules(self, rules: Sequence[Rule]) -> None:
        """Add rules to the live set (the user-rule install path).

        Callers run on the event-loop thread (MCP tool handlers, the
        boot scan); the drain scans each flow in a worker thread, one
        at a time between awaits, and queued entries keep their
        arrival-time snapshots, so the set is never observed
        mid-mutation. Applies forward only: flows that arrived before
        the registration never match the new rules. Registering builds
        the next ruleset epoch — the new rules' body patterns compile
        into a fresh database on this thread, off the scan path (the
        drain keeps scanning queued entries against the epochs they
        arrived under) — and the body memo clears with it: every memo
        key carries the epoch serial (ticket 14).
        """
        self._rules.extend(rules)
        self._epoch_serial += 1
        self._epoch = _RulesetEpoch(self._rules, self._epoch_serial)
        self._memo.clear()

    def deregister_rules(self, rule_ids: AbstractSet[str]) -> None:
        """Drop every live rule whose id is in ``rule_ids`` (the remove path).

        Flows already queued keep their arrival-time snapshot; findings
        already recorded are never retracted — removal stops future
        findings only — and the next ruleset epoch compiles without
        the removed rules' patterns, clearing the body memo with it
        (every memo key carries the epoch serial, ticket 14).
        """
        self._rules = [rule for rule in self._rules if rule.id not in rule_ids]
        self._epoch_serial += 1
        self._epoch = _RulesetEpoch(self._rules, self._epoch_serial)
        self._memo.clear()

    async def run(self) -> None:
        """Drain queued flow records against the rules until cancelled.

        The task the MCP lifespan owns: started before the session
        serves, cancelled at teardown. Each flow's scan runs in a
        worker thread (one flow at a time, in queue order) so scan
        cost — linear in body size, seconds for multi-MB JS — never
        blocks the loop serving tool calls (ticket 08). Cancellation
        stops the drain between flows; one in-flight scan may still
        finish (and append its finding) past the cancel. A flow
        enqueued but not drained when the session dies is never
        scanned — the accepted loss of the enqueue-and-drain model
        (ADR-0034).
        """
        self._loop = asyncio.get_running_loop()
        # Anything enqueued before the task's first tick still drains.
        self._wakeup.set()
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            while True:
                try:
                    record, epoch = self._queue.get_nowait()
                except queue.Empty:
                    break
                # Off the loop thread: scan cost is linear in body size
                # (multi-MB JS bodies scan for seconds, ticket 08), and
                # the loop here serves every concurrent MCP tool call.
                # Still strictly one flow at a time, in queue order —
                # each scan is awaited before the next dequeue.
                await asyncio.to_thread(self._scan, record, epoch)

    def _scan(self, record: dict[str, Any], epoch: _RulesetEpoch) -> None:
        """Scan one flow record against its arrival-time ruleset epoch."""
        flow_id = str(record.get("id", "?"))
        view = FlowView(self._data_dir, record)
        # The body stage is memoized per (epoch, stored-body digest)
        # (ticket 14): a repeated body replays every rule's pre-gate
        # body-stage result instead of rescanning. Evidence assembly
        # beyond the body and Finding emission stay per flow, so two
        # flows with the same body record their own findings, and the
        # memo is invisible to everything except drain time.
        digest = self._body_digest(view) if epoch.memoizable else None
        stage = self._body_stage(view, epoch, digest)
        for index, rule in enumerate(epoch.rules):
            try:
                # Dataset-backed rules (ADR-0035) override Rule.scan:
                # the vendored data's extractors and per-advisory
                # severities cannot fold into the declarative spec, so
                # they hand back fully formed findings.
                findings = self._scan_rule(rule, view, epoch, digest, index)
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
        """Digest of the flow's stored body bytes, or ``None``.

        SHA-256 over the file on disk — the exact bytes every stage of
        the scan reads — hashed in 1 MiB chunks so the digest read
        drops the GIL between slices (the drain worker is already off
        the loop thread; the chunking keeps the hash from hitching any
        thread that shares the GIL). An unreadable body has no digest
        and takes the uncached path — the body read itself then fails
        exactly where it always did.
        """
        body_file = view.record.get("body_file")
        if body_file is None:
            return None
        hasher = hashlib.sha256()
        try:
            for chunk in read_chunked(self._data_dir / body_file, _HASH_CHUNK):
                hasher.update(chunk)
        except Exception:
            # Unreadable or malformed record: no digest, uncached path
            # — the body read itself then fails where it always did,
            # contained per rule.
            return None
        return hasher.digest()

    def _body_stage(
        self, view: FlowView, epoch: _RulesetEpoch, digest: bytes | None
    ) -> _BodyStage:
        """The epoch's body-stage results for one flow's body, memoized.

        The cold path reads the body once, runs the single engine pass
        (``find_all`` releases the GIL), and assembles every rule's
        pre-gate body evidence — all the expensive work, now done once
        per (epoch, body). A repeat of the same body under the same
        epoch replays the whole stage from the memo. A body that
        cannot be read (or scanned) fails here once and is re-raised
        per rule at the point the all-stdlib path raised it; errors
        are not memoized — an unreadable body has no digest to key on.
        """
        if not epoch.scans_body:
            return _BodyStage(None, ())
        key = _StageKey(epoch.serial, digest) if digest is not None else None
        if key is not None:
            cached = self._memo.get(key)
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
            )
        except Exception as exc:
            # Read or scan failure (and a raising pattern, which the
            # old path contained per rule the same way): fail here
            # once, re-raise per rule — never memoized.
            return _BodyStage(exc, ())
        if key is not None:
            self._memo.put(key, stage, _stage_size(stage))
        return stage

    def _scan_rule(
        self,
        rule: Rule,
        view: FlowView,
        epoch: _RulesetEpoch,
        digest: bytes | None,
        index: int,
    ) -> list[Finding] | None:
        """Run one rule's ``scan()`` override, memoized per body (ticket 14).

        The override is an opaque body of work — the retire.js corpus
        pass above all — so the memo caches its whole result under
        (epoch, body digest, flow context, rule index) and re-stamps
        each cached Finding with the current flow's id on replay: a
        duplicate pays the digest read instead of the dataset scan,
        and still records its own findings. The flow-context half of
        the key (``_scan_context``) is everything such an override
        lawfully reads per flow — its own gates (retire.js's
        content-type screen) and the record's stable routing fields —
        so a flow the override gates out never replays a gated-in
        flow's findings.
        """
        if digest is None or type(rule).scan is Rule.scan:
            return rule.scan(view)
        key = _ScanKey(epoch.serial, digest, _scan_context(view), index)
        cached = self._memo.get(key)
        if cached is not _MEMO_MISS:
            flow_id = str(view.record.get("id", "?"))
            return [replace(finding, flow_id=flow_id) for finding in cached]
        findings = rule.scan(view)
        if findings is not None:
            frozen = tuple(findings)
            self._memo.put(key, frozen, _findings_size(frozen))
        return findings

    def _match(
        self,
        rule: Rule,
        view: FlowView,
        stage: _BodyStage,
        index: int,
    ) -> list[Evidence]:
        """Apply a rule's gates, then its match spec, to one flow view.

        The body half replays the rule's memoized pre-gate body-stage
        result (ticket 14); the gates — flow context — filter here,
        per flow, so one cached body-stage entry byte-identically
        serves flows the gates disagree on.
        """
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
        """Record one finding; a failing write is logged and skipped."""
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
            # Lazy data-dir precedent (ADR-0017): the scanner subtree
            # appears on the first finding, never at boot.
            self._findings_path.parent.mkdir(parents=True, exist_ok=True)
            # One single-shot write on an append-mode binary handle:
            # the whole line lands in one write(2), so a sibling
            # session's appends can never interleave mid-line (the
            # findings index is append-atomic, per the PRD).
            with self._findings_path.open("ab") as fh:
                fh.write(payload)
        except Exception:
            logger.exception(
                "scanner could not record finding for rule %r; skipped",
                finding.rule_id,
            )


def _content_type_matches(specs: tuple[str, ...], actual: str | None) -> bool:
    """Check a content-type gate: prefix match, parameters stripped."""
    mime = (actual or "").split(";", 1)[0].strip().lower()
    return bool(mime) and any(mime.startswith(spec) for spec in specs)


def _pattern_evidence(
    patterns: tuple[re.Pattern[str], ...], text: str, where: str
) -> list[Evidence]:
    """All matches of the patterns against text, as evidence entries.

    Each pattern runs its necessary-literal gate first (ticket 08):
    a gate miss on a large body skips the regex scan — one C call
    that would hold the GIL for the whole linear pass.
    """
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
    """Engine spans as evidence entries — the find-iterator's records.

    The spans are the stdlib find-iterator's by construction (the
    seam's parity contract), so the slice is ``match.group(0)`` and
    the start is ``match.start()``.
    """
    return [
        Evidence(
            where=_RESPONSE_BODY,
            offset=start,
            detail=text[start:end][:_EVIDENCE_DETAIL_MAX],
        )
        for start, end in spans
    ]


def _stored_response_headers(view: FlowView) -> str | None:
    """The stored response headers, or ``None`` when the flow has none.

    Error flows never wrote a ``response_headers`` file; a header rule
    simply has nothing to look at, which is a skip, not a failure.
    """
    try:
        return view.response_headers
    except FileNotFoundError:
        return None


def _passes_gates(rule: Rule, record: dict[str, Any], view: FlowView) -> bool:
    """Every set gate passes; an unset gate passes all flows.

    Unset gates deliberately include error flows: a rule with no
    status or content-type gate scans flows without a response side
    too, which is how request-side rules reach error flows (their
    request file exists even though nothing came back).
    """
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
    """The request-side pattern pair after its own browser gate.

    ``request_browser_only`` gates only the request-side matchers, so
    one rule can mix halves: an ungated response matcher next to a
    request matcher that trusts browser-produced requests only (the
    Xdebug rule's shape — Set-Cookie echo ungated, request-param half
    gated).
    """
    if rule.request_browser_only and not browser:
        return (), ()
    return rule.request_query_patterns, rule.request_header_patterns


def _response_evidence(
    rule: Rule,
    view: FlowView,
    stage: _BodyStage,
    index: int,
) -> list[Evidence]:
    """Body and response-header matches; absent material is a skip.

    The body half is the rule's memoized pre-gate body-stage result
    (ticket 14) — assembled once per (epoch, body) by
    ``_rule_body_evidence`` and replayed per flow, in the rule's
    body_patterns order, byte-identical to the all-stdlib scan. The
    index into ``stage.body_evidence`` happens only for a rule that
    carries body patterns (an epoch that scans no bodies stores an
    empty row), after the error re-raise. The header half is per-flow
    stored material. Unreadable body: fail this rule where the old
    path did.
    """
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
    """Query-parameter-name and request-header-line matches."""
    evidence: list[Evidence] = []
    if query_patterns:
        evidence += _pair_evidence(query_patterns, _query_pairs(view), _REQUEST_QUERY)
    if request_header_patterns:
        evidence += _pair_evidence(
            request_header_patterns, _request_header_lines(view), _REQUEST_HEADERS
        )
    return evidence


def _query_pairs(view: FlowView) -> list[tuple[str, str]]:
    """Query parameters as ``(match text, evidence detail)`` pairs.

    Patterns match parameter names — the name is the signal — while
    the recorded detail is the whole ``name=value`` pair so a finding
    names the offending parameter. A flow without a stored request
    file has nothing to match, which is a skip, not a failure.
    """
    try:
        pairs = view.request_query
    except FileNotFoundError:
        return []
    return [(name, f"{name}={value}") for name, value in pairs]


def _request_header_lines(view: FlowView) -> list[tuple[str, str]]:
    """Request header lines as ``(match text, evidence detail)`` pairs.

    Both are the stored ``Name: value`` form (verbatim case, value
    included — the credential itself is the evidence). A flow without
    a stored request file is a skip, not a failure.
    """
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
    """One evidence entry per (item, pattern) match; offset is the index.

    The request side matches parsed items, not one text blob, so an
    evidence entry identifies its item by position in the parsed list
    and by the pair's rendered detail.
    """
    return [
        Evidence(where=where, offset=index, detail=detail[:_EVIDENCE_DETAIL_MAX])
        for index, (text, detail) in enumerate(items)
        for pattern in patterns
        if pattern.search(text)
    ]
