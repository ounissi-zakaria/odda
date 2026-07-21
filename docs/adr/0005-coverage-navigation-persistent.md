# Coverage is navigation-persistent; ADR-0004 applies to Wrap and Logpoint only

ADR-0004 established that Dynamic analysis records are wiped on navigation. That rule
applies to Wrap and Logpoint, which record discrete events — accumulating events from
unrelated page loads mixes noise the agent cannot tell apart. Coverage is carved out of
that rule: Coverage is an aggregate query over a browsing context (per `CONTEXT.md`),
and its primary workflow is `start` → `navigate` (to trigger the behavior under
investigation) → `snapshot`/`stop`. Wiping the recording on navigation breaks that
workflow, since the most common frontend behavior is code that runs as a consequence of
navigating (URL-parameter consumers, hashchange handlers, SPA route handlers). The
recording window is `[start, stop]` regardless of how many navigations happen inside it;
counts for the same script URL merge across loads.

The rejected alternative is mirroring Wrap/Logpoint: wipe the accumulator on navigate but
keep the recording alive so the agent does not have to re-`start`. This gives per-load
counts directly, but throws away the cross-load total — which is the question the agent is
usually asking. Per-load slicing is still available under the chosen design via snapshot
subtraction, so the carve-out is strictly more expressive. Coverage still does not survive
tab close, matching Logpoint's per-tab-session lifecycle.