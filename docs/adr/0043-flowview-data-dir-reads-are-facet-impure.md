# FlowView carries the data dir; scans that read it never memoize

A `scan()` override that needs cross-flow context — a sibling body to
compare against, the flows index for what the session already saw —
had no lawful way to find it: FlowView exposes one flow's parsed
material and nothing about where flows live, so rules cwd-guessed the
data dir. The guess worked only because `project_data_dir()`
resolves `.odda` against the same working directory the rule code
runs under, an accident no contract protected. FlowView now exposes
`data_dir`: the project's data dir (`.odda`), the root the scanned
flows live under, documented in the rule guide beside the properties
that parse one flow — the index is `data_dir / "flows" /
"flows.jsonl"` (one record dict per line), a sibling body is
`data_dir / record["body_file"]`.

The property is a context read, not a facet read, and
`DATASET_SCAN_READS` says so by omission: `data_dir` is deliberately
absent from the memoizer's allowlist, so the subset gate in the scan
path (reads within `DATASET_SCAN_READS`, else no memoization) makes
any scan that touches it non-memoizable — fresh scan per flow, the
same discipline request-side reads already carry. Cross-flow context
does not merely vary between flows that share a facet; it grows as
flows arrive and spans sessions — the index holds flows the current
session never wrote and will never scan — so a cached result must
never answer for a flow it never saw. No new machinery: the read
tracker and the gate pre-existed; the property only joins them.

A raw path over a structured cross-flow API is the deliberate half
of this. Rules already run as arbitrary Python in the same trust
boundary as proxy-scripts (ADR-0035, ADR-0036): the path grants no
capability a rule lacked — it grants correctness — and the on-disk
layout is already an agent-facing documented surface (the server
instructions carry `flows.jsonl` and `flows/<id>/`). A structured API
(iterate the index, fetch a body by id) would freeze a larger
contract to serve one known need, and the records it handed back
would still be the index lines, so the layout would be coupled either
way.

The decision is recorded because a raw path on a class of parsed
reads is surprising without its reason; because the memoization
consequence is invisible in findings — a wrongly-memoized cross-flow
scan emits stale evidence, not an error; and because the raw-path
choice had a genuine structured alternative.
