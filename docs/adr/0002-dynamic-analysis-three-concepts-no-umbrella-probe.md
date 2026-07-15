# Dynamic analysis uses three direct concepts — Wrap, Logpoint, Coverage — with no umbrella "Probe"

The dynamic analysis feature exposes three peer concepts under the Dynamic analysis umbrella:
Wrap (placed observation at a function/property via userscript), Logpoint (placed observation
at a source location via non-pausing `Debugger.setBreakpointByUrl`), and Coverage (aggregate
block-level hit counts via `Profiler`). An umbrella "Probe" concept was considered and rejected.

The rejection reason: Wrap and Logpoint share an *intent* (placed observation, records discrete
events) but not a *mechanism* (userscript replacement vs CDP breakpoint) and not a *target language*
(a function/property name vs a URL+line+column). The agent's decision tree branches on what it
knows — "I know the function" → Wrap, "I know the line" → Logpoint, "I know neither" → Coverage —
so the agent picks directly from its state, never by first choosing an umbrella then a variant.
An umbrella "Probe" would add a noun the agent never uses for a decision it never makes.

The naming also rejects the overloaded terms "trace" (collides with the command-group name, the
umbrella, and the specific primitive) and "breakpoint" (implies pausing, which was dropped).
"Wrap" was chosen over "Hook" (overloaded with React hooks and webhooks), "Logpoint" over
"breakpoint"/"tracepoint" (implies pausing or is vague), and "Coverage" over "profile" (CDP
already uses "Profiler" for the domain; "Coverage" is the user-facing concept).