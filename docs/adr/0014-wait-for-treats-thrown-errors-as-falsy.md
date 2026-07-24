# `wait-for` treats a thrown error as falsy and keeps polling

`odda wait-for` wraps the agent-supplied expression in a `try`/`catch`
inside the polling function (`browser.py:wait_for`): `() => { try {
const v = (<expr>); return v ? v : false; } catch (e) { return false; } }`.
Any thrown error — a `TypeError` from a null deref
(`document.querySelector('#root').children` while `#root` is absent),
a `ReferenceError` on a symbol a lazy bundle hasn't defined yet, or
any other exception — becomes falsy and polling continues until the
expression is truthy or the timeout fires. This inverts Playwright's
default, where a throw inside `wait_for_function` propagates as a
hard error; the whole point of `wait-for` is "the DOM isn't ready
yet," and a null deref is the common pre-DOM-ready case, not a fatal
error.

The narrower alternative — catch only `TypeError` (the feedback's
literal ask, #1) — would have failed fast on a `ReferenceError` from
a typo'd symbol name. We rejected it because a `ReferenceError` on a
symbol that *will* be defined later (a global from a lazy/split
bundle that hasn't loaded yet) is also a "not ready yet" condition,
not a bug in the expression. Catch-all matches Playwright's own
recommended pattern for `wait_for_function` and treats every throw
as "keep waiting"; a genuinely broken expression still fails —
cleanly, via timeout — rather than silently succeeding. The cost is
that a typo'd symbol name polls for the full timeout instead of
erroring in milliseconds, trading a fast failure for never crashing
on a transient "not ready" state.

## Consequences

- `wait-for` no longer crashes on a null deref or any throw inside the
  expression; it polls until truthy or timeout. Agents writing
  wait-for expressions for pre-DOM-ready conditions need no defensive
  `document.querySelector('#x') && ...` guard.
- A genuinely broken expression (syntax that throws on every poll)
  now costs a full timeout before failing, where before it failed
  on the first poll. This is the intended trade: prefer "keep
  waiting" over "crash on a transient state."
- The catch-all is documented in SKILL.md's `wait-for` entry so
  agents know errors are falsy, not fatal.