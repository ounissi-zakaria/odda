# Wrap is leaf-only — it records the call it was placed on, not callbacks passed as arguments

A Wrap placed on `EventTarget.prototype.addEventListener` records each registration call
(`{type, fn, stack}`) but does not follow the callback function reference and install a
sub-Wrap on it. To see what a registered handler does when invoked, the agent uses Coverage
to find the handler's code path and a Logpoint to read locals at the interesting line.

The rejected alternative was automatic callback-following: when `addEventListener('message', cb)`
is captured, also wrap `cb` so its invocation is recorded. This was rejected because it composes
two different behaviors under one command (the agent can't ask for Wrap-without-follow), and
because the three-concept workflow already covers the use case: Wrap confirms registration,
Coverage finds the path, Logpoint reads the locals. Adding callback-following would duplicate
the Coverage+Logpoint path with a less controllable mechanism (every event type's callbacks
get wrapped, not just the one the agent cares about).

Leaf-only is reversible: a `--follow` flag or a separate "Follow" concept can be added later
without breaking existing Wraps.

Leaf-only means a Wrap does not *invoke* callbacks for the agent, but the
wrap serializer does capture the callback's source code (`.toString()`,
capped at 1000 chars) in the record's `args`/`ret` slot as
`{type: "function", name, source}`. Reading the source from the record
is not a substitute for the Coverage+Logpoint path — it tells the agent
*what* the callback is, not *what it did when invoked* — but it is
enough to decide whether to follow it. The cap keeps records bounded
for fat arrow functions with large bodies.