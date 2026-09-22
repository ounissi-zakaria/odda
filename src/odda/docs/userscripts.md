# Userscripts

The `userscript_*` tools manage JavaScript helpers that auto-run at `document_start` on every navigation, before the page's own scripts, in the main world. Install a helper once and it runs before the page's own scripts on every `navigate` and `tabs_open`. Useful for both browser automation (inject helpers) and dynamic analysis (Wraps are userscripts — see `odda://docs/dynamic-analysis`). This is the reference for storage internals and built-in defaults.

## Tools

- `userscript_install(browser_id, name, file=None, source=None, force=False)` — Install a userscript into the given browser's scope, from a JS file path (`file`) or inline source (`source`) — exactly one of the two. Overwrites any existing userscript of the same name and reloads that browser's extension.
- `userscript_list(browser_id)` — List installed userscripts for the given browser with their names and sizes.
- `userscript_remove(browser_id, name)` — Remove a userscript from the given browser's scope and reload its extension. The script's effects on the current page are not undone; it won't run on future navigations.

## Storage and scope

Userscripts are stored **per-browser** on disk under `.odda/browsers/<browser_id>/userscripts/<name>/script.js`. Each browser gets its own Chrome extension that inlines that browser's installed userscripts and is loaded when that browser is opened. A userscript installed on one browser never reaches another — `browser_id` is the scope key, and identifiers are five-letter tokens never assigned to a different browser than the one that first claimed them, so a new browser can never inherit an old browser's scripts. A browser you open starts with only the default userscripts (below); reopening a closed browser (see `browser_open`) brings its installed userscripts back with it.

## Behavior notes

- **Before page scripts.** Userscripts run at `document_start`, so `window` modifications are visible to the page before any of its own scripts execute. This is the key advantage over `eval` (which runs after navigation).
- **All tabs and frames of one browser.** A userscript runs in every tab and every frame (including iframes) of the browser whose scope it was installed into.
- **Idempotent re-injection.** Scripts run on every navigation. Write them to be idempotent (e.g. guard with `if (window.__myHelper__) return;`).
- **Reload applies on next navigation.** `userscript_install`/`userscript_remove` reload the given browser's extension, but already-loaded tabs are not re-injected. Re-navigate an existing tab (or open a new one) for the change to take effect there.

## Built-in default userscripts

odda ships built-in default userscripts that are always injected before any installed userscripts in every browser. They are inlined into the same extension as installed userscripts, so they load in every tab and frame at `document_start`, ahead of the page's own scripts — but they are not listed by `userscript_list` (which reports installed scripts only) and cannot be removed.

Currently the default is a single **logpoint helper**: it sets up `window.__oddaLogpoint` (the per-tab record array that `logpoint_dump` reads) and the value-serialization helper `window.__oddaSerialize` used by logpoint breakpoint conditions. It guards its own definitions (`if (!window.__odda…)`) so re-injection is idempotent, and because defaults load before installed userscripts, the default's definition always wins — do not define your own `window.__oddaLogpoint`, `window.__oddaLogpointPush`, or `window.__oddaSerialize` in an installed userscript expecting it to take effect. Overwriting `window.__oddaLogpoint` with a non-array breaks `logpoint_dump` on that tab.