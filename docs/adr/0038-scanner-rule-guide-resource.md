# The rule guide is the one docs resource

Removal history first. The six `odda://docs/*` resources were deleted
whole (b3c3b9d): five taught what tool results already show, and the
sixth's real contract (wire-faithful vs frame-source request crafting)
moved into the tool descriptions. CODING_STANDARDS codified the
principle — agent-facing text lives in four surfaces (tool
descriptions, server instructions, tool results, error messages) and
nowhere else — and that principle was right for everything it covered:
surfaces are where agents already look, and most teaching is a
constraint that belongs in the error that fires.

Rule authoring broke the rule. After the de-bundle (ADR-0036), the
23-line rule-file skeleton inside `rule_install`'s description was the
entire authoring surface. A description cannot carry the FlowView API,
`scan()` override semantics (an override owns its gates — the
declarative gates never run for it), the severity scale's discipline,
permanent-id rationale, arrival-time install semantics, or the
engine-compatibility tradeoff of ADR-0037; an install error can only
repeat the file shape at the point of failure; and no agent can author
a dataset-style rule — the highest-value kind, the retire.js slot's own
shape — from a skeleton alone. The content is pages, the surfaces are
lines, and the reader has no repo access to fall back on.

So: one MCP resource, `odda://scanner/rule-guide`, carries the full
authoring guide — complete Rule field reference (every pattern kind,
every gate, the severity enum), the FlowView API derived from the
model, worked examples of both rule styles (the override mirroring the
retire.js slot), the severity scale, permanent-id discipline,
arrival-time install semantics, and the engine-compatibility tradeoff.
`rule_install`'s description shrinks to the install contract plus a
one-line pointer to the resource. The skeleton stays single-sourced:
the guide's rule-file section is generated from the same
`RULE_FILE_SKELETON` constant every load-failure rejection appends, so
the guide and the errors cannot drift (the e2e suite pins the
equality, and the payload's section coverage, from the client side).

Storage follows the removal's own precedent. The prose lives in
`src/odda/scanner/rule-guide.md`, shipped as package data and read
through a thin loader at serve time — the same shape the removed
`odda://docs/*` resources used (markdown in `src/odda/docs/`,
b3c3b9d), recovered because pages of prose are reviewable and
editable as markdown, not as a Python f-string with doubled braces
and escaped backslashes. Only the splice stays dynamic: the md
carries a `{{RULE_FILE_SKELETON}}` placeholder, the loader binds the
constant into it, and the client-side equality pin guards the splice.

CODING_STANDARDS gains the exception test that keeps this from
reopening the door the removal closed: a docs resource may exist only
for content no tool surface can carry and an agent cannot proceed
without. The rule guide passes — authoring requires the API reference
and the examples, and no line-shaped surface holds them. Everything
that fails the test goes to the four surfaces, where it belongs.

The decision is recorded because it is a recorded reversal: the
standards said no docs resources, and now one exists. The reversal is
narrow (one resource, one test, codified), not a return to the
six-resource sprawl — and this record is where any future "should X be
a resource?" question restarts from: apply the test, not the precedent.
