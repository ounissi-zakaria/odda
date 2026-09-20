# Screenshot returns inline pixels by default

`screenshot` used to be a pure str tool: the result text was the written
JPEG path (the ADR 0023 text-first contract), and a vision-capable
harness paid one extra round trip per screenshot to see the pixels
(`read <path>`). Text-only models gain nothing from that economy — they
cannot use pixels at all, and the path text survives whatever else is on
the wire.

Decision: **`screenshot` inlines the JPEG as an `ImageContent` block by
default** — `[TextContent(path), ImageContent(image/jpeg, data)]` — with
`return_image=false` restoring the bare path-only result. The path stays
the first text block in both modes: a text-only harness collapses the
image block to a placeholder and keeps the addressable handle
(`read <path>?q=<question>` delegates to the vision role), so the
path-as-delegation-handle story is unchanged. Verified against omp
18.2.6: image blocks are filtered per request (placeholder substituted),
non-destructively — the payload survives a later model switch.

## Considered options

- **Opt-in flag** (`return_image=true`, the research sketch's original
  recommendation): preserved 0023's default at the cost of the agent
  knowing a flag exists; a vision model that forgets the flag burns the
  round trip this exists to remove. Overruled: on-by-default.
- **Harness-side stripping only** (no flag): pushes odda's contract onto
  client behavior — a raw MCP client without image filtering would face
  the block with no opt-out.
- **Content negotiation**: MCP has no model-capability handshake; the
  server cannot and need not know the model.

## Consequences

- `screenshot` leaves the "text channel only" class of 0023's 13
  flattened tools: the text channel is still the path (first block), but
  the default result carries a second, non-text block, and the return
  annotation widened to `str | CallToolResult`. `structured_output=False`
  is unchanged — no `outputSchema`, no structured channel.
- The image block's bytes are the persisted file's bytes verbatim
  (read-after-write; no re-encode). JPEG, viewport-only — unchanged.
- Wire-shape matrix and omp mechanics (placeholder text, `blob:` refs,
  `?q=` delegation): `.scratch/screenshot-image-return/research/
  screenshot-image-return.md`.
