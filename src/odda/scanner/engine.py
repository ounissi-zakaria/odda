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
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
from pathlib import Path
from typing import TYPE_CHECKING, Any

from odda.scanner.model import Evidence, Finding, FlowView

if TYPE_CHECKING:
    import re
    from collections.abc import AbstractSet, Sequence

    from odda.scanner.model import Correlator, Rule

logger = logging.getLogger(__name__)

# Matched text kept per evidence entry — enough to identify and grep,
# bounded so a pathological body cannot bloat the findings line.
_EVIDENCE_DETAIL_MAX = 200

# Evidence "where" for matches in the stored response body.
_RESPONSE_BODY = "response_body"
# Evidence "where" for matches in the stored response headers.
_RESPONSE_HEADERS = "response_headers"


class Scanner:
    """Session-live passive scanner.

    ``observe`` is the writer's post-append hook (enqueue only);
    ``run`` is the lifespan-owned drain task. Findings append to
    ``<data_dir>/scanner/findings.jsonl``, one line per (rule, flow),
    evidence aggregated. Zero egress by construction: the scanner only
    reads captured material and writes the findings index.
    """

    def __init__(
        self,
        data_dir: Path,
        rules: Sequence[Rule] = (),
        correlators: Sequence[Correlator] = (),
    ) -> None:
        """Initialize with the session's data dir and the ruleset.

        Args:
            data_dir: The session's ``.odda`` directory; the scanner's
                subtree (``scanner/findings.jsonl``) appears lazily
                under it on the first finding.
            rules: Rules every observed flow is checked against.
            correlators: Cross-flow analysis seam (none ship).
        """
        self._data_dir = Path(data_dir)
        # Mutable live set: register/deregister (user-rule installs and
        # removes, ticket #06) run on the event-loop thread; the drain
        # only ever iterates an arrival-time snapshot (see observe), so
        # the list is never read mid-mutation.
        self._rules: list[Rule] = list(rules)
        self._correlators: tuple[Correlator, ...] = tuple(correlators)
        self._queue: queue.SimpleQueue[tuple[dict[str, Any], tuple[Rule, ...]]] = (
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
            self._queue.put((record, tuple(self._rules)))
            loop = self._loop
            if loop is not None:
                loop.call_soon_threadsafe(self._wakeup.set)
        except Exception:
            logger.exception("scanner enqueue failed; flow write unaffected")

    def register_rules(self, rules: Sequence[Rule]) -> None:
        """Add rules to the live set (the user-rule install path).

        Callers run on the event-loop thread (MCP tool handlers, the
        boot scan); the drain scans synchronously between awaits and
        queued entries keep their arrival-time snapshots, so the set is
        never observed mid-mutation. Applies forward only: flows that
        arrived before the registration never match the new rules.
        """
        self._rules.extend(rules)

    def deregister_rules(self, rule_ids: AbstractSet[str]) -> None:
        """Drop every live rule whose id is in ``rule_ids`` (the remove path).

        Flows already queued keep their arrival-time snapshot; findings
        already recorded are never retracted — removal stops future
        findings only.
        """
        self._rules = [rule for rule in self._rules if rule.id not in rule_ids]

    async def run(self) -> None:
        """Drain queued flow records against the rules until cancelled.

        The task the MCP lifespan owns: started before the session
        serves, cancelled at teardown. A flow enqueued but not drained
        when the session dies is never scanned — the accepted loss of
        the enqueue-and-drain model (ADR-0034).
        """
        self._loop = asyncio.get_running_loop()
        # Anything enqueued before the task's first tick still drains.
        self._wakeup.set()
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            while True:
                try:
                    record, rules = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._scan(record, rules)

    def _scan(self, record: dict[str, Any], rules: tuple[Rule, ...]) -> None:
        """Scan one flow record against its arrival-time rule snapshot."""
        flow_id = str(record.get("id", "?"))
        view = FlowView(self._data_dir, record)
        for rule in rules:
            try:
                evidence = self._match(rule, view)
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
        for correlator in self._correlators:
            try:
                findings = correlator.observe(view)
            except Exception:
                logger.exception(
                    "scanner correlator %r failed on flow %s; skipped",
                    correlator.id,
                    flow_id,
                )
                continue
            for finding in findings:
                self._emit(finding)

    def _match(self, rule: Rule, view: FlowView) -> list[Evidence]:
        """Apply a rule's gates, then its match spec, to one flow view."""
        record = view.record
        if rule.schemes and record.get("scheme") not in rule.schemes:
            return []
        if rule.statuses and record.get("status_code") not in rule.statuses:
            return []
        if rule.content_types and not _content_type_matches(
            rule.content_types, view.content_type
        ):
            return []
        if rule.browser_only and record.get("browser_id") is None:
            return []
        if not rule.body_patterns and not rule.header_patterns:
            return []
        evidence: list[Evidence] = []
        if rule.body_patterns:
            body = view.body
            if body is not None:
                evidence += _pattern_evidence(rule.body_patterns, body, _RESPONSE_BODY)
        if rule.header_patterns:
            headers = _stored_response_headers(view)
            if headers is not None:
                evidence += _pattern_evidence(
                    rule.header_patterns, headers, _RESPONSE_HEADERS
                )
        return evidence

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
            line = json.dumps(record, ensure_ascii=False) + "\n"
            # Lazy data-dir precedent (ADR-0017): the scanner subtree
            # appears on the first finding, never at boot.
            self._findings_path.parent.mkdir(parents=True, exist_ok=True)
            with self._findings_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
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
    """All matches of the patterns against text, as evidence entries."""
    return [
        Evidence(
            where=where,
            offset=match.start(),
            detail=match.group(0)[:_EVIDENCE_DETAIL_MAX],
        )
        for pattern in patterns
        for match in pattern.finditer(text)
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
