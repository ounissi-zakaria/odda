# Userscripts

`odda userscript` manages JavaScript helpers that auto-run at `document_start` on every navigation, before the page's own scripts, in the main world. Install a helper once and it runs before the page's own scripts on every `odda navigate` and `odda tabs open`. Useful for both browser automation (inject helpers) and dynamic analysis (Wraps are userscripts — see [DYNAMIC-ANALYSIS.md](DYNAMIC-ANALYSIS.md)). This is the reference for storage internals and built-in defaults; see [SKILL.md](SKILL.md) for the command surface and Targeting model.

## Commands

- `odda userscript install --name <name> --file <path>` — Install a JS file as a userscript into the given browser's scope. Overwrites any existing userscript of the same name and reloads that browser's extension. Alternatively, use `--source "<js>"` for inline source (mutually exclusive with `--file`).
- `odda userscript list` — List installed userscripts for the given browser with their names and sizes.
- `odda userscript remove --name <name>` — Remove a userscript from the given browser's scope and reload its extension. The script's effects on the current page are not undone; it won't run on future navigations.

## Storage and scope (per ADR-0010)

Userscripts are stored **per-browser** on disk under `.odda/browsers/<browser_id>/userscripts/<name>/script.js`. Each browser gets its own Chrome extension at `.odda/browsers/<browser_id>/userscripts-extension/` with a `content.js` that inlines that browser's installed userscripts (each wrapped in try/catch). The extension is loaded via CDP `Extensions.loadUnpacked` when that browser is opened. A userscript installed on browser 1 does not reach browser 2 — `--browser-id` is the scope key. An agent opening a fresh browser starts with only the default userscripts (below).

## Behavior notes

- **Before page scripts.** Userscripts run at `document_start`, so `window` modifications are visible to the page before any of its own scripts execute. This is the key advantage over `odda eval` (which runs after navigation).
- **All tabs and frames of one browser.** The extension's content script matches `<all_urls>` and runs in all frames (`all_frames: true`), applying to every tab in the browser whose scope it was installed into.
- **Idempotent re-injection.** Scripts run on every navigation. Write them to be idempotent (e.g., guard with `if (window.__myHelper__) return;`).
- **Reload applies on next navigation.** `install`/`remove` reload the given browser's extension, but already-loaded tabs are not re-injected. Re-navigate an existing tab (or open a new one) for the change to take effect there.

## Built-in default userscripts

odda ships built-in default userscripts that are always injected before any installed userscripts in every browser. Currently this includes a **dialog interceptor** that records calls to `window.print`, `window.alert`, `window.confirm`, and `window.prompt` in `window.__oddaDialogs`. It loads at `document_start` before the page's own scripts, so it captures alerts from page scripts, userscripts, and payload scripts injected later (e.g. via prototype pollution `data:` URL gadgets).

`odda eval --js "window.__oddaDialogs"` returns an array of captured dialog/print events. Each entry has `{type, url, timestamp, stack, result?}` plus `message` and `defaultValue` when applicable:

- `type` is one of `print`, `alert`, `confirm`, `prompt`.
- `message` is present for `alert`/`confirm`/`prompt`.
- `defaultValue` is present for `prompt`.
- `result` is recorded for `confirm`/`prompt` — the value the interceptor returned (a registered override if set, else the default; see below).

Use this to inspect what modal dialogs or print calls a page triggered during automation.

### Dialog responses: proceed-by-default and override

`confirm` and `prompt` **proceed by default** (ADR-0011): `confirm` returns `true` and `prompt` returns `"odda"`, so a dialog-gated form or action proceeds instead of being silently denied by headless Chrome's native handlers. `alert` (forwards to the native no-op) and `print` (suppressed) are unchanged — they have no return value to gate.

Override the return value per-type by setting `window.__oddaDialogResponses` before the triggering call:

```
odda eval --js "window.__oddaDialogResponses = {prompt: 's3cr3t'}" --browser-id <B> --tab-id <T>
odda page click --ref e8 --browser-id <B> --tab-id <T>
odda eval --js "window.__oddaDialogs" --browser-id <B> --tab-id <T>   # result: "s3cr3t"
```

- The map is keyed by dialog type (`{prompt: "value", confirm: true}`). Registered values pass through **verbatim** — no type coercion; `{confirm: "yes"}` returns the string `"yes"` (truthy, page proceeds as confirmed).
- To restore the old deny-by-default per-type, register `{confirm: false, prompt: null}`.
- Keys for `alert`/`print` are ignored (no return value to influence).
- The interceptor **reads** `window.__oddaDialogResponses` only; it never writes or resets the map. The map is agent-owned state. This preserves the on-load-prompt escape hatch: a userscript setting the map at `document_start` runs before the page's on-load `prompt()`, so the interceptor sees the registered value. (Default userscripts load before agent-installed ones, so an interceptor that reset the map would clobber an agent userscript's response.)
- The map **persists until cleared** (re-`eval` `window.__oddaDialogResponses = {}` or `delete` the key). A forgotten response leaks to later dialogs of that type, but the leak is visible in `__oddaDialogs` (every dialog records its `result`).

#### Limitations

- **Cross-frame.** The interceptor runs in all frames (`all_frames: true`), but `odda eval` sets `window.__oddaDialogResponses` on the top frame's `window` only. A `prompt()` inside a cross-origin iframe won't see a registered response and gets the default (`"odda"`). This is the same frame limitation `window.__oddaDialogs` recording has — whatever the agent does for `__oddaDialogs` (frame-targeted eval) it does for `__oddaDialogResponses`.
- **Cross-navigation (userscript-set responses).** A response set via `eval` is scoped to one document and dies on navigation (fresh `window`). A response set via a userscript (the on-load-prompt escape hatch) persists across navigation and applies to an unrelated page's on-load prompt too. This is inherent to how userscripts work (run on every navigation; see "Idempotent re-injection" above). Prefer `eval` for one-shot responses; reserve userscript-set responses for pages whose on-load scripts call `confirm`/`prompt`.