# The body stage scans one Vectorscan database per epoch; incompatible patterns prefilter and confirm

The scanner's response-body stage runs on the Vectorscan-backed
`hyperscan` CPython extension. The dependency is deliberate and hard:
it releases the GIL around its scan calls (a 10 MB body passes in one
call without pinning the event loop's thread), ships binary wheels for
every supported CPython on Linux/macOS/Windows, and there is no
fallback engine to compile without it. A fallback would double the
parity surface — two engines whose findings must stay byte-identical —
for a benefit (install reach) that wheels already cover.

One compiled database serves one ruleset epoch. The epoch — the
immutable rules tuple plus its database, snapshotted at each flow's
arrival — is the scan unit: boot and every slot install, remove, or
force-swap build the next epoch off the scan path, and a flow is always
scanned by exactly the database of the epoch it arrived under. Every
declarative body pattern of the epoch compiles into that one database,
so each body is scanned once, whatever the rule count.

Patterns Vectorscan cannot scan exactly — backreferences (the Prometheus
exposition, agent-authored quote-pair secrets), lookarounds (the
compromised-CDN import shapes), word boundaries, possessive repeats —
compile into the same database as approximate prefilters (the engine's
prefilter mode: over-reporting allowed, under-reporting impossible).
Their spans are confirmed: the true stdlib pattern runs, and only on
bodies whose approximation hit, replacing the approximation's spans
with the find-iterator's own. The approximation is a necessary
condition, never a sufficient one, so findings stay byte-identical to
the all-stdlib path, and a body no approximation hits leaves the
one-pass database without a single stdlib body scan. Flow-context gates
(scheme, status, content type, attribution) still filter after the
pass, per rule, before evidence assembly. Confirmation happens inside
the body stage's single find-all, on the drain's worker thread (the
ticket-10 offload): the confirmed spans are part of the body-stage
result every rule — and any later memoization of that result — shares.
On non-ASCII bodies the engine's Unicode tables are not Python's, so an
approximation that comes back empty is stood in for by the true
pattern's own search before the no-hit conclusion is drawn; the superset
guarantee never rests on the divergent tables alone.

Patterns that can match the empty string are the boundary of this
design: Vectorscan refuses them at build (`FastScan.build` raises,
naming the pattern), and no approximation exists for them —
zero-width spans are exactly what an end-offset engine cannot report.
They stay on the gated stdlib residual path, as do patterns refused by
the expansion screens below. The necessary-literal gates remain the
residual path's own pre-regex optimization.

The compile-expansion hazard shaped those screens. Vectorscan compiles
a bounded repeat by unrolling it, one state per step, with no cost
bound it exposes: measured on this corpus, jquery's `.{50,300}`
compiles in 2.3 s, tinyMCE's `.{0,1200}` in 79 s, and vue's
`[^\n]{0,8000}` never finished within 90 s — a hang, not a slowdown,
sitting on the register-path thread. Two screens keep such shapes off
the engine: the engine's expansion estimate (repeats multiply,
concatenation adds, literals are free; anything over the budget stays
stdlib) and the retire.js slot's own shape screen over the vendored
dataset (wide categories under large bounds, deeply nested bounded
repeats). Both only ever pick a path — findings are byte-identical on
either side of the screen.

The screens leave a measured, deliberate residual on the stdlib path:
of the vendored dataset's 207 filecontent patterns, 47 stay off the
one-pass database — overwhelmingly bounded-repeat shapes whose
unrolling compiles run the 2.3 s–79 s–never spectrum above, which no
boot or install can pay. This is the one accepted exception to "one
pass per body", bounded twice over: the shape screens only ever pick
a path (screened-out patterns lose speed, never findings), and each
residual scan runs behind its necessary-literal gate, so an unmatched
body — the overwhelming case — pays one fastsearch containment check
per pattern and no regex pass. Revisit if a dataset pattern family
ever dominates scan time in a real session; until then the residual
is the cheaper side of the trade.

Catastrophic backtracking inside a confirmation is accepted for now:
there is no timeout and no match budget around the stdlib confirm. The
rejected alternative was a process pool — a worker with its own GIL,
killable on a wedge — rejected because it pays always (pickling
multi-MB bodies per flow, process spawn and churn per session, the
drain's one-flow-at-a-time ordering and per-rule containment both
complicated by cross-process boundaries) to insure against a hazard
that has not materialized (the confirming patterns are the corpus's
own, bounded shapes). The revisit trigger is a real session wedge
attributed to a confirmation; if it fires, the pool — or a match
budget inside the confirm — is the change to make, and this record is
where that decision restarts from.

The decision is recorded because the engine's promise — findings
byte-identical to stdlib, at millisecond loop cadence, for every
pattern an author can write — is the product of four interacting
parts (the hard dependency, per-epoch compilation, prefilter +
confirm, the expansion screens) that individually look like
complexity without cause. ADR-0035's rule-author surface is unchanged
by all of it: an author writes Python regexes; whether a pattern scans
exactly, approximates and confirms, or stays stdlib is invisible in
the findings.
