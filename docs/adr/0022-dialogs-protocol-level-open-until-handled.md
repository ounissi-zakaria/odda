# Dialogs are protocol-level, open until handled

odda stops pre-empting JavaScript dialogs in-page. The built-in dialog
interceptor userscript (`override-dialogs.js`, which monkey-patched
`window.print/alert/confirm/prompt` at `document_start`, returned
proceed-by-default values, and recorded calls into
`window.__oddaDialogs`) is deleted; dialogs become first-class protocol
events (`page.on("dialog")`), stay open until handled, and are resolved
only by the new `dialog_handle` tool (`accept`/`dismiss`,
`prompt_text`) or by a human closing them in a headed window. The
deleted interceptor's proceed-by-default policy is reversed, and the
`__oddaDialogResponses` response map dies with it.

The trigger rule is the load-bearing part: the action that *opens* a
dialog (`page_click`, `eval`, `wait_for`, `navigate`, `tabs_open` —
including a `beforeunload` on the page being left, and the initial
goto of a newly opened tab) returns immediately with the
dialog's `{type, message, default_value, tab_id}` instead of hanging;
actions issued *while* the dialog is open on the same tab reject
immediately with an anticipated error naming the dialog (the
agent retries after handling; amended 2026-09-06 from the original
"block indefinitely" park semantics — a parked call gives the
agent no feedback and dies as a host-side timeout when never
handled). Without the trigger rule the design deadlocks: a
triggering action never finishes by itself (the renderer is frozen
inside the `confirm()` call — verified on the patchright 1.62.1 pin
with a prototype; Playwright's own docs state it: "the page will freeze
waiting for the dialog, and actions like click will never finish"), so
an agent waiting on the click would never learn a dialog opened and
would never call `dialog_handle`. This is the failure class playwright-mcp
shipped (its `browser_navigate` still hangs on dialogs, issue #1279);
we do not repeat it. The per-browser `dialogs` note on every
same-browser result was also repealed 2026-09-06: the trigger result
reports the dialog on the tab that opened it and the reject error
names it on every same-tab touch, so a cross-tab broadcast had no
consumer. `tabs_close` on a dialog tab closes it and reports
`closed_dialog: {type, message}`. `window.print` is unpatched: in
headless mode it is a Chrome no-op; in headed mode it may open the OS
print dialog (the one un-handleable dialog type — there is no protocol
event for it; suppression was possible only via the in-page patch we
deleted).

Why reject instead of proceed-by-default: the interceptor optimized
for flow (a dialog-gated submission proceeds in one call), but the cost
was auto-proceeding into potentially destructive actions, no fidelity
(the patch only saw calls through the patched `window.*` properties),
beforeunload was invisible (it silently hung `navigate` for the full
timeout — an unpatchable event), and cross-origin iframe dialogs were
recorded but unreadable. Protocol-level dialogs have none of those
blind spots, and `dialog_handle` gives the agent the real message
*before* deciding — the one thing proceed-by-default structurally
cannot do. The cost is a second tool call per dialog. The original
"unattended session blocks indefinitely" hang class is gone with the
2026-09-06 reject semantics: an ignored dialog now fails every
same-tab call with the anticipated error instead of parking it.

## Consequences

- `window.__oddaDialogs` / `window.__oddaDialogResponses` /
  `__oddaDialogInterceptorInstalled` are gone; agents read dialog state
  from tool results, not in-page arrays. Dialog call *stacks* are no
  longer captured by default (a wrap on `confirm`/`alert`/`prompt` can
  recover them — wraps call through to the original).
- `dialog_handle` on a tab with no open dialog errors; handling is
  one-dialog-at-a-time per tab.
- `beforeunload` becomes visible and handleable (it used to be a
  silent `navigate` hang); `browser_close`/`tabs_close` semantics are
  unchanged (plain `page.close()`, beforeunload does not fire).
- User handling is detected by liveness (page JS resumes when the
  dialog closes); patchright 1.62.x has no `dialogclosed` event, so
  odda cannot distinguish *how* the dialog was closed, only that it
  was. Verified on the pin with a real human clicking the native
  dialog in a headed window: the prompt genuinely shows for the human
  (the pin driver never calls `Page.setInterceptDialogs`, so nothing
  suppresses it), the click resolves page JS with the real return
  value (`confirm()` → true), and the parked probe completes exactly
  when the dialog closes.
- After a user close the driver's dialog state is stale
  (`hasOpenDialogsForPage` stays true). The poisoning is benign:
  `page.title()` degrades to `''` (the pin's `_title()` catches its own
  dialog-gate error and returns empty), while fresh `evaluate` and
  `aria_snapshot` bypass the gate and work normally. odda purges the
  stale state best-effort on probe completion — call the parked
  `Dialog.accept()` and suppress the deterministic protocol error
  (`No dialog is showing`); the failed call still purges, because the
  pin's `_accept` marks the dialog handled and removes it from
  `_openedDialogs` *before* the CDP send (self-healing, verified: titles
  restore immediately after the suppressed call).
- `dialog_handle` racing a user close: if the browser-level close lands
  first, odda's accept raises the same protocol error and
  `dialog_handle` reports an anticipated error — the dialog on tab X
  was already closed (handled by the user or closed in the browser);
  the attribution is deliberately neutral because liveness only proves
  *that* the dialog closed, not *who* closed it. If odda's accept
  lands first, the native dialog vanishes before the human can click it
  — no double-handle is possible.
