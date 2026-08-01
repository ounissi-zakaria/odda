# `browser open` defaults to headless; `--headed` opts into a visible window

`odda browser open` launches Chrome headless by default. The previous
`--headless` flag (default off, i.e. headed) is removed and replaced by
`--headed` (default off, i.e. headless). The JSON-RPC `browser/open`
param stays `headless` (matching patchright's own parameter name); the
CLI inverts before sending: `headless = not headed`.

The primary user of odda is an autonomous agent that does not need a
visible window. A headed default meant every agent-driven `browser open`
popped a Chrome window that disrupted the human working alongside the
agent and added friction for no benefit. Flipping the default makes the
common case (agent runs unattended) the zero-flag case, and reserves the
flag for the human who actually wants to watch (`--headed`, for
interactive debugging).

This is a hard break, recorded for the same reason as ADR-0009: the
previous flag (`--headless`) is removed entirely, not kept as an alias.
Every test doc, recipe, and the shared browser-fixture lib was updated
in the same change — the fixture now relies on the headless default
rather than asserting it. A future contributor who "helpfully" re-adds
`--headless` would reintroduce a second flag for one axis, the clutter
ADR-0009 reacted against. The trade-off is that anyone with muscle memory
of `--headless` gets an error on upgrade; the error is self-explanatory
(`--help` shows `--headed`), and the population is small.