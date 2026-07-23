# Dialog interceptor is proceed-by-default for confirm and prompt

The built-in dialog interceptor's default return values for `confirm`
and `prompt` are `true` and `"odda"` respectively, not the native
headless-Chrome returns of `false` and `null`. The previous defaults
silently swallowed dialog-gated submissions: a `confirm("Submit?")`
returned `false`, so the page did nothing, with no signal to the agent
beyond a later inspection of `window.__oddaDialogs`. The new defaults
let the page proceed so the agent can observe the resulting behavior
(flow, DOM, follow-up requests) instead of debugging a no-op.

The response map `window.__oddaDialogResponses` remains the single
override mechanism: registering `{confirm: false, prompt: null}`
restores the old deny-by-default per-type. An agent who needs a
specific `prompt` answer (the PRD's motivating case — `prompt("Answer:")`
expecting `s3cr3t`) still pre-registers it; the `"odda"` default only
covers prompts where any non-null value is acceptable.

The decision is recorded because it breaks the PRD's original
"backward compatible: no map set = current behavior" promise and
reverses a prior default — a future reader seeing `confirm()` return
`true` with no map set would wonder why, and a contributor might
"restore" the old null/false defaults thinking they were a bug. The
`alert`/`print` handlers are unchanged (no return value to gate;
`print` is still suppressed, `alert` still forwards to the native
no-op).