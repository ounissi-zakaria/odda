# `browser_open` defaults to headless

`browser_open(headless=True)` launches Chrome headless by default; an
agent that wants a visible window passes `headless=False`. The param
name matches patchright's own parameter name.

The primary user of odda is an autonomous agent that does not need a
visible window. A headed default meant every agent-driven
`browser_open` popped a Chrome window that disrupted the human working
alongside the agent and added friction for no benefit. The headless
default makes the common case (agent runs unattended) the zero-arg
case, and reserves `headless=False` for the human who actually wants to
watch (interactive debugging).

Headless is one boolean param for one axis. A future contributor who
"helpfully" adds a second surface for the same axis (a
`browser_open_headed` tool, a `--headed`-style flag on some other
layer) reintroduces the two-flags-one-axis clutter this default
exists to avoid. The trade-off is that anyone wanting to watch the
window must pass the explicit param; the cost is one argument.