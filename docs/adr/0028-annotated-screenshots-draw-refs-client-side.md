# Annotated screenshots draw refs client-side

`screenshot(annotate=true)` draws every snapshot ref's bounding box and its
`[ref=eN]` label onto the captured JPEG: pixels and refs in one artifact, so a
vision-capable harness can pick coordinates for `page_click`/`page_hover`
coordinate targeting on things the a11y tree can't name (canvas, overlays,
custom hit-testing). It is the visual counterpart of `page_snapshot`'s
`boxes=true`; the result contract is otherwise untouched — path as the first
text block, inline pixels by default (ADR-0027).

The decisions:

- **Param named `annotate`, not `boxes`.** `boxes` already has a textual
  contract on `page_snapshot`/`page_find` ("emit `[box=x,y,w,h]` per line").
  On screenshot the flag renders boxes into an image instead of returning
  them — same word plus a different output modality would muddy the
  vocabulary.
- **Compositing happens client-side (Pillow), never in-page.** The screenshot
  is evidence in a security-research tool: capture must be a pure read.
  DOM-overlay injection (Betterwright's `screenshot({annotate: true})`
  approach) is visible to MutationObservers and anti-DOM-tamper logic, needs
  cleanup on every failure path (crash mid-way = overlay left until
  navigation), and cannot reach cross-origin iframes — betterwright had to
  offset child-frame boxes into top-document coordinates, which is exactly
  what the driver's `aria_snapshot(boxes=true)` already hands us. Injection
  would reimplement the driver for no gain. The CDP `Overlay` domain was
  rejected in kind: the DevTools highlight overlay is excluded from
  `Page.captureScreenshot` output — it is not a rendering path.
- **The geometry read does not touch Diff baselines.** Boxes come from
  `page.aria_snapshot(mode="ai", depth=None, boxes=True)` inside the
  screenshot's own `_run_action` wrapper — not through `page_snapshot`, whose
  render would become the tab's diff baseline for its depth (ADR-0026). Only
  a `page_snapshot` moves baselines; a screenshot silently resetting them
  would make a following `page_snapshot(diff=true)` falsely report "nothing
  changed".
- **The result carries no annotation note.** Earlier drafts returned an
  "`annotated N refs`" line so a zero-ref capture would not read as a silent
  no-op. Dropped: the result shape stays exactly the plain call's (path,
  plus the inline image when `return_image`), and a zero-ref annotate is
  visually self-evident to the model reading the pixels.
- **Chips use the snapshot syntax** — `[ref=e3]`, `[ref=f2e5]` — so the label
  on the pixels is exactly the value the agent passes to `page_click`.

## Considered options

- **DOM-overlay injection** (Betterwright parity): rejected — see above.
- **CDP `Overlay` domain**: rejected — not a rendering path.
- **`boxes` param name**: rejected — collides with the textual contract.
- **`annotated N refs` result note**: rejected — one more result shape to
  document and test for a signal the pixels already carry.
- **`refs: list[str]` label filter**: deferred — the natural follow-up if
  dense pages get too noisy; `page_find` stays the narrowing tool for now.
- **`full_page` annotated captures**: out of scope — page-coordinate boxes
  would need scroll-offset handling; viewport-only keeps the coordinate
  spaces aligned by construction.

## Consequences

- `pillow` is a runtime dependency. The annotate path captures PNG bytes into
  memory, draws, and encodes JPEG once at save time (`quality=80`); the
  plain path is byte-identical to before (driver writes the JPEG directly).
- Box coordinates, stroke width (2px `#FF00FF`), and the label chip
  (filled magenta, black text, `ImageFont.load_default(size=13×dsf)`) all
  scale by `window.devicePixelRatio` read per capture — the screenshot
  rasterizes at the device scale factor while the driver reports CSS px.
  The chip's black-on-magenta text was chosen over the sketched
  white-on-magenta by A/B on rendered samples: 6.6:1 vs 3.2:1 contrast, so
  labels survive JPEG loss and harness-side image downscaling. In odda's
  own launches DSF is always 1 and the MCP surface offers no knob to force
  otherwise, so the e2e suite exercises the multiply only at 1.0 (accepted
  residual risk).
- The dialog gate is `_require_ready_tab` at method entry, and none of the
  annotate sub-calls (capture, geometry read, DSF read) can open a dialog —
  one gate covers the whole shoot, and the tab's blocking behavior is
  unchanged from the plain capture.
- Snapshot lines without a box, or with a zero-size box, are skipped rather
  than drawn.
- The snapshot/capture race (page mutating between the capture and the
  geometry read) is accepted: it already exists when an agent calls
  `page_snapshot` then `screenshot` as two tool calls; annotate collapses it
  to one round trip and shrinks the window.
- Cross-references: `page_snapshot`/`page_find` `boxes` docstrings point at
  annotate; the screenshot docstring names `boxes=true` as its counterpart;
  the glossary folds annotation into the **Screenshot** term (avoid-list:
  overlay, ref overlay, labeled screenshot).
