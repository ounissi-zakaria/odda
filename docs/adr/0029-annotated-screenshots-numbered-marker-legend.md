# Annotated screenshots carry a numbered-marker legend

ADR-0028 shipped `screenshot(annotate=true)` with text chips — each box
labeled `[ref=eN] x=…,y=…` on the pixels. Task-time review of the rendered
samples showed the chip's own payload was the noise source: ~130px of page
occluded per element, chip collisions on dense layouts, and long strings
blurring under harness-side image downscaling — eroding exactly the
legibility the annotation exists to provide.

Decision: **the chip carries only a marker number; the values move into a
CSV legend delivered as a second text block in the same response** —
`n,ref,x,y,w,h`, one row per drawn marker, snapshot order, 1-based. Pixels
keep the association (which box wears which number — vision's strength);
text carries the exact values (text's strength). The legend is generated
in the same loop that draws, so marker↔ref↔box cannot desync:
misattribution becomes structurally impossible instead of rare. Marker
numbers are per-capture labels with the same lifetime as the image — they
are not Refs and mean nothing outside their response.

This supersedes ADR-0028's chip-content decisions (snapshot-syntax chips,
origin coordinates in the label) and amends its no-note rationale: the
struck `annotated N refs` line was decorative status, while the legend is
load-bearing payload — the image is unusable without it. The invariant
that mattered survives: the path is still exactly the first text block. A
zero-ref capture omits the legend block entirely and returns the plain
call's shape.

Format: CSV — chosen over JSON (less punctuation noise for the model
reading raw text) and over reusing the snapshot tag syntax (a table
parses by code with no regex). `w`/`h` ride in the legend for free:
on pixels they were the occlusion budget; in text they cost nothing and
enable box-center targeting.

## Consequences

- Result matrix: plain = `[path]` or `[path, image]`; annotated =
  `[path, legend]` or `[path, legend, image]`. `structured_output=False`
  unchanged (ADR-0023) — every block is still text or image, no schema.
- Marker chips are ~6× narrower than the text chips; the `refs` filter
  remains the lever for pages so dense that even markers collide.
- The e2e test asserts the legend's boxes against
  `page_snapshot(boxes=true)`'s own `[box=…]` values for the same refs —
  one driver source, two renderings must agree.
- Marker numbers are assigned in snapshot order at capture time; a
  re-annotate may renumber. Agents re-read the legend every call — the
  mapping is never assumed to persist.
