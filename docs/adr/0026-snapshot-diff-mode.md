# Snapshot diff mode — depth-keyed baselines, geometry-free comparison, `-`/`+` hunks with ancestor headers

`page_snapshot(diff=true)` returns only the lines that changed since the
tab's previous snapshot **at the same depth** — the post-action re-read
is the most frequent snapshot call in a session, and full-tree re-reads
dominate tool-result tokens. Decisions, each grounded in measurements
taken on a 1301-line synthetic tree (978 ref-bearing elements):

- **Baselines key on `depth` alone, not `(depth, boxes)`.** Comparison
  strips `[ref=…]`, `[box=…]`, and ADR-0025's `[deeper=k]` tags plus
  trailing note before matching, so a boxes and a non-boxes snapshot of
  the same depth are comparable and an agent flip-flopping the flag never
  silently degrades a diff to a full tree. Box-stripping is mandatory
  anyway: boxes are viewport-relative `getBoundingClientRect` integers —
  verified: `scrollBy(0, 500)` turned `[box=8,55,…]` into
  `[box=8,-424,…]` — so any scroll rewrites every box on the page and a
  geometry-aware comparison would report the whole page as changed.
- **`boxes` stays opt-in.** Always-on boxes were rejected: 978 boxed
  lines × ~20 chars ≈ +45% bytes (≈ +5k tokens) on every snapshot,
  find, and baseline — a standing tax that fights the feature's purpose,
  paid for geometry that Coordinate targeting needs occasionally. `-`/`+`
  lines are emitted verbatim from their respective renders (old baseline
  / fresh render); a `+` line carries boxes exactly when the diff call
  asked for them.
- **Refs are sticky — verified.** Inserting an element early in the DOM
  assigned fresh refs after the running max (`e979`–`e981`) and left
  every existing ref untouched; the aria-ref counter is monotonic per
  page lifetime, not positional. Ref-stripping in comparison is kept as
  a safety net, not a correctness requirement.
- **Every `page_snapshot` stores its render as the new baseline for its
  depth** — diffs chain against the immediately previous state.
  `page_find` renders internally and never touches baselines. First diff
  with no baseline returns the full tree announced by a one-line note
  (`(no previous snapshot for this depth — full tree stored as diff
  baseline)`), stored as the baseline; no changes returns the sentinel
  `(no changes since previous snapshot)`.
- **Any frame navigation in the tab clears its baselines** (tab close
  frees them with the tab object). Without the wipe, a post-navigation
  diff would emit the whole old tree as `-` plus the whole new tree as
  `+` — double a full snapshot in tokens, the blowup the feature exists
  to prevent. Same-document history changes don't navigate; the diff
  just reports the DOM delta.
- **Output format: `-`/`+` prefix with the original line intact**
  (marker is the first char; indentation, YAML dash, and content follow
  verbatim — fresh refs on `+`, old refs on `-`), **no context lines**
  (unchanged content is never re-sent), and **one ancestor-path header
  per contiguous hunk** in page_find's existing `[n] a > b > c`
  convention — the new tree's path when the hunk has `+` lines, the old
  tree's for pure deletions. The bare `-`/`+` prefix stacks on the
  snapshot format's own YAML list dashes; that collision is why the
  no-baseline case is annotated rather than silent.

Rejected alternatives:

- *Always-on boxes to "simplify" the shape key* — the premise inverted:
  comparison must strip geometry regardless (scroll churn), so `boxes`
  adds no diff complexity, only the standing token tax.
- *Unified-diff ±N context lines* — re-sends unchanged content, the
  exact cost the feature exists to avoid; the ancestor header answers
  "where" in one line per hunk.
- *Bracketed `[-]`/`[+]` markers* — unambiguous against YAML dashes but
  a novel format; the annotated no-baseline case plus first-char markers
  resolve the ambiguity without inventing one.
- *Only non-diff calls update the baseline* — repeated diffs would
  re-report old changes against a stale reference point.
- *No navigation wipe* — full old-tree→new-tree transition diffs,
  double a full snapshot.

Recorded because the cost model is counterintuitive in both directions:
dropping box-stripping "for strictness" makes every scroll a full-page
diff, and making boxes always-on "for simplicity" taxes every snapshot
~45% for a feature that needs them rarely.
