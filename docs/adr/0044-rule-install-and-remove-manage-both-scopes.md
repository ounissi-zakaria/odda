# rule_install and rule_remove manage both scopes

This supersedes the hand-managed-global half of ADR-0036: "global
slots change only by hand", "no tool writes the global rules dir",
and that ADR's closing trust boundary — the mutation tools act only
on the project the session owns. Everything else in ADR-0036
stands: the two-scope layout, boot loading of both, rule ids unique
across both because they key the findings index, disables as
project state that never touches the global dir, and the
no-bundled-rules posture.

`rule_install` and `rule_remove` take a `scope` parameter — project
(the default, the data dir's rules tree) or global (the config
home's rules dir). The reversal exists so a session can build and
manage a shared machine-global corpus with the same tool surface it
uses for project rules, instead of an operator file-drop being the
only path; hand placement remains and is identical to a tool
install. Removal is symmetric for a reason: a slot a tool can
install is a slot a tool can remove — an install-only asymmetry
would leave tool-created global slots unmanageable by tool, strictly
worse than the hand-managed world it replaced.

The carried-over semantics do the containment. Rule ids stay unique
across both scopes, rejected at install naming the collision.
Arrival-time epochs (ADR-0034) apply unchanged: a global install
fires in the installing session from arrival and reaches other
projects at their next boot — exactly the visibility a hand-placed
slot always had. Disables stay project state: a project suppresses
a global rule locally, and `rule_disable`/`rule_enable` never touch
the global dir.

The blast radius is accepted and named: an agent session writing
rules that fire in every project on the machine is now one tool call
with an explicit, labeled parameter (`scope="global"`) rather than
an out-of-band operator act. The boundary shifts from "tools never
write global state" to "the global write is explicit and scoped" —
which is where the boundary was doing its real work, since the
default project scope keeps every unqualified call project-local.

The decision is recorded because it reverses a recorded decision
whose ADR explicitly anticipated the missing-feature reading; and
because future readers hitting "no tool writes it" in ADR-0036 or
older prose need to know which decision is current.
