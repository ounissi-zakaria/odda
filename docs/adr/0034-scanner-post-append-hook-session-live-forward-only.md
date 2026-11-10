# The scanner observes flows through the FlowRecordWriter post-append hook, session-live and forward-only

The passive scanner receives each flow at the moment it is written: a hook
fired by `FlowRecordWriter` after the `flows.jsonl` line lands (when the
per-flow request, response, and error files are all durable) enqueues the
record, and an asyncio task owned by the MCP lifespan drains the queue and
scans. Findings append to `.odda/scanner/findings.jsonl`. Three invariants
are definitional: zero egress (nothing is ever sent — analysis of captured
material only), forward-only (a flow that predates the scanner's start is
never analyzed; there is no catch-up or backfill, ever), and per-session
observation (each flow is scanned exactly once, by the session that wrote
it — the data dir is shared project state, but observation is not).

The rejected alternative was tailing `flows.jsonl` (polling or inotify via
watchdog). A tailer reads the same durable bytes and stays decoupled from
writer internals, but with two concurrent MCP sessions in one data dir — a
case odda already supports for browser records — both tailers see every new
line and systematically double-scan every flow, duplicating every finding
while both live; and a tailer needs position state (or a full rescan per
boot) to know where to resume. Hooking the writer makes per-flow attribution
structural: the session that wrote the flow scans it, matching the
session-isolation model in `CONTEXT.md`, and no position state of any kind
exists. Inline scanning at the hook point was also rejected: the mitmproxy
addon writes flows from the proxy's own thread, and a body-regex rule over
a large response (measured worst case ~33 MB/s) could stall proxy traffic
for hundreds of milliseconds — hence enqueue-and-drain, with scan failures
logged and skipped so a broken rule can never threaten a flow write.

Costs accepted: the scanner depends on the writer's threading model (one
queue handoff seam), and a flow whose session died before its scan is never
scanned by anyone — the same loss a crashed tailer would leave behind, and
repairing it would require exactly the backfill this decision forbids.

The decision is recorded because both invariants look like bugs to a future
contributor: "the scanner skips flows.jsonl history" invites a catch-up
"fix", and "two sessions don't rescan each other's flows" invites a shared
position state — either change silently reintroduces cross-session
duplication or unbounded boot-time work that the per-session hook model
exists to avoid.
