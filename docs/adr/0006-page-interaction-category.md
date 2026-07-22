# Add Page interaction as a second category alongside Dynamic analysis

odda's surface was observation-only through its first five ADRs: Wrap, Logpoint, and Coverage
all record what code does without modifying the page (ADR-0002 framed this as the defining intent:
"observe rather than modify"). This ADR adds a peer category, Page interaction, that *does*
modify the page — `snapshot`, `click`, `fill`, `hover`, `upload` — backed by Playwright's
`page.aria_snapshot(mode="ai")` and the `aria-ref` selector engine.

The category exists because the agent's existing path for driving the page — `odda eval` with
hand-written CSS selectors — is brittle on the minified React SPAs that are odda's typical
targets, and because the alternative (snapshot + refs) is essentially free in patchright:
Playwright already implements the a11y-tree serialization, the ref-to-element mapping, the
cross-iframe encoding (`f<seq>e<seq>`), and the actionability/auto-wait plumbing. The cost is a
new detection surface (synthetic input events via CDP `Input.dispatch*`), which patchright
already accepts as the cost of using Playwright at all.

The five new commands live under a new `odda page` sub-app. Existing page-action commands
(`navigate`, `eval`, `screenshot`, `wait-for`, `event-listeners`) stay flat for backwards
compatibility — the asymmetry is deliberate: the new commands form a cohesive concept cluster
(snapshot + ref-driven actions) that earns a sub-app, while the existing flat commands were
never grouped and renaming them would break every existing script and test doc for no gain.

Page interaction is both a standalone feature (the agent uses odda purely for browser automation)
and a composing step with Dynamic analysis (snapshot → click to trigger → Wrap/Coverage/Logpoint
to observe what the click handler did). The canonical recipe in SKILL.md grows to include the
trigger step.