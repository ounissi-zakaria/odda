# Browser identifiers are project-lifetime random tokens

A browser is named by a five-letter lowercase token (`a`–`z`, drawn from
`secrets`), unique for the lifetime of the data dir and never reused.
The token appears in the same places the old per-session integer did —
the `browser_id` parameter on every browser-scoped tool, the
`browser_id` field on `flows.jsonl` records, and the
`.odda/browsers/<token>/` per-browser storage — with the type changed
from `int` to `str`. Tool input is case-insensitive and normalized to
lowercase. `.odda/browsers/` is never cleaned: leftovers from dead
sessions are inert because no future browser can ever receive a token
that has been used before. Generation collision-checks against live
instances and on-disk dirs and retries.

This replaces a per-session integer counter starting at 1. That scheme
leaked state across sessions: `.odda/browsers/<id>/` persists in the
project data dir while the counter reset, so `sync_extension` at
browser open re-inlined the previous session's userscripts into the
new session's browser 1 — contradicting the documented fresh-browser
semantics. The same reset also made `browser_id` values in
`flows.jsonl` ambiguous across sessions, since the index accumulates
in one project dir while ids recycled.

The trade-offs: tokens are unsortable and carry no density or ordering
information (accepted — flows are ordered by flow id, browsers have no
meaningful order); the alphabet-only space is 26⁵ ≈ 11.9M, small
enough that generation must collision-check (accepted — retry makes
reuse exactly zero); `.odda/browsers/` grows a few KB per browser
forever (accepted — hygiene only, correctness never depended on
cleanup); and every agent-facing surface breaks its int typing at once
(accepted at 0.x, before the surface had external dependents).

The decision is recorded because the token format and its
never-reused promise are load-bearing for storage layout and index
semantics: a contributor who "simplifies" generation back to a
per-session counter silently reintroduces the userscript leak, and
nothing in the code makes that coupling obvious.
