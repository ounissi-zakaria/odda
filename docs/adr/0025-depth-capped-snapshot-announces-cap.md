# Depth-capped snapshots announce their cap — boundary `[deeper=k]` tags plus a trailing real-depth note

`page_snapshot(..., depth=N)` renders the a11y tree only down to level N,
but the result never said so: nodes at the cut render childless and are
byte-indistinguishable from genuine leaves, so a capped tree looks
complete. An agent reading it acts on partial information — concludes an
element doesn't exist, picks a wrong target, or burns a turn
re-discovering the page. Now, when `depth` is set and the tree actually
reaches the cap, the result announces the cut: each boundary line gets a
`[deeper=k]` tag (levels hidden below the cap, in the same units as the
`depth` parameter) and the result ends with a blank line plus

```
[snapshot capped at depth N — tree is D levels deep, L lines hidden;
raise depth to see everything, or page_find to search without the full
tree]
```

`D` is the tree's real depth — the smallest `depth` value that renders
everything. `L` counts hidden lines (not nodes): lines are what the text
channel costs, and lines are what the two renders make countable. Trees
that fit under the cap — including the false alarm where the deepest
nodes are genuine leaves sitting exactly at the cap — are returned
byte-untouched. `depth=None` (the default, and `page_find`'s internal
call) never annotates; `boxes: true` composes, with `[deeper=k]` landing
after the `[box=...]` tag.

The facts come from one extra **uncapped render, server-side, text
discarded**: the driver (patchright, pinned) walks the whole page on
every snapshot regardless of `depth` — the cap only gates rendering — so
the walk is never saved by capping, and only a second render can see
past it. The driver exposes no depth metadata on its text-only channel
(`renderAriaTree` returns `{text, iframeDepths}`; Python gets `str`),
and patching the pinned driver bundle is not on the table. The probe is
gated: it runs only when the capped text's deepest content line sits
exactly at the cap (prop lines render one level past their node even at
the cap, so they don't count — a gate that counted them would probe on
every capped call). The probe renders with boxes off; alignment strips
`[box=...]` tags so geometry differences between the renders never read
as drift.

Failure posture: if the probe errors (tab navigated mid-probe) or the
two renders disagree on line identity (page mutated in between), the
capped tree is returned as-is — silent cap, the pre-feature behavior —
never a failed tool call, never numbers from a stale tree.

Rejected alternatives:

- *Second render at `depth+1`, detection only* — cheaper probe, but
  cannot produce `D` or per-boundary `k` (only "≥1 hidden level"), and
  the per-boundary tag is the actionable half of the feature.
- *No probe; hedge "may be capped" whenever lines reach the cap* —
  false-alarms on deep-but-fitting trees, training agents to ignore the
  note.
- *Deriving real depth from the capped text alone* (indent arithmetic) —
  impossible: the renderer clamps output at the cap and prints cut nodes
  exactly like leaves; the uncapped depth is not in the string.
- *Patching the injected driver script to surface depth metadata* —
  violates the pin; every patchright bump would silently revert it.
- *Reporting hidden node counts* — nodes aren't countable from rendered
  text (text children merge into their parent's line); lines are.

This is recorded because the cost model is counterintuitive and easy to
"optimize" into a lie: the natural-looking fix — skip the probe, guess
from indents — silently reintroduces the exact failure (a tree that
looks complete but isn't), and the gate's prop-line exemption looks
like a bug to anyone who hasn't read the renderer.

Forward note for the `page_snapshot` diff mode (`.scratch/page-snapshot-diff/`):
baselines must store the unannotated tree — strip `[deeper=k]` tags and
the trailing note line — or every capped snapshot diffs against a
shifting annotation instead of the tree.
