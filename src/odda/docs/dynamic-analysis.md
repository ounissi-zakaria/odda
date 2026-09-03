# Dynamic analysis

Observing JavaScript execution in progress — recording what code does as it runs, with the intent to observe rather than modify. Three peer concepts, chosen by what you know (there is deliberately no umbrella "Probe" noun — pick directly from your state, never by first choosing an umbrella then a variant):

- **Wrap** — "I know the function/property" → wrap it and record each call/access.
- **Logpoint** — "I know the line" → plant a non-pausing observation at a source location.
- **Coverage** — "I know neither" → record which code blocks execute, then find the path.

All tools in this file are tab-scoped: every call takes `browser_id` and `tab_id`.

## Shared: record lifecycle and scope

Applies to Wrap and Logpoint (Coverage is navigation-persistent — see its section below).

- **Records wipe on navigation.** Records from the previous page load are gone after a navigate. **Dump before navigating again** or the records are lost.
- **Scope: main frame and same-origin iframes.** Both Wrap and Logpoint reach the main frame and same-origin iframes. Wrap **also** reaches cross-origin iframes (records stay in the iframe's context — read them by evaluating in the iframe); Logpoint **does not** reach cross-origin iframes. Neither reaches worker contexts (web workers, service workers). Installations are per-browser (see `odda://userscripts`).
- `wrap_remove` / `logpoint_remove` on an unknown name/id errors.

## Shared: command shape

Wrap and Logpoint share an `add`/`list`/`remove`/`dump`/`clear` tool set:

- `*_add` — install. Takes effect on the next navigation (re-navigate the tab or open a new one) for Wrap; binds on add for Logpoint.
- `*_list` — list installed wraps/logpoints for the given browser.
- `*_remove` — remove an installation; stops recording on future hits.
- `*_dump` — read the per-tab record array. Returns the records captured since the last navigation/clear.
- `*_clear` — zero the per-tab record array without navigating. Returns `{status: "cleared", count: <records dropped>}`. Installations are unaffected; subsequent calls continue to record.

## Shared: serialization rules

Both Wrap and Logpoint record values are serialized via the same serializer. Applies to `args`, `ret`, `this` (Wrap), and `value` (Logpoint).

- **Functions** (in `args`, `ret`, or `this`): serialized as `{type: "function", name: "<inferred name or null>", source: "<.toString() capped at 1000 chars>"}`. The `source` field is enough to read what a callback does and decide whether to follow it with Coverage+Logpoint, but you cannot invoke captured functions. Bound/anonymous functions may have a `source` of `function () { [native code] }` or `bound ` — that is the function's own `toString()`, not odda's.
- **Large or cyclic values**: truncated and marked. Objects with more than 50 keys become `{type: "object", truncated: true, keys: [...]}`. Arrays longer than 100 become `{type: "array", truncated: true, length: N}`. Strings longer than 10000 characters become `{type: "string", truncated: true, length: N, preview: "..."}`. Cycles become `{type: "object", truncated: true, cycle: true}`. Depth beyond 5 levels is truncated.
- **Call stack frames** (Wrap only): one shape `{fn, url, line, col}`; native or eval frames leave `url`/`line`/`col` null. Extension frames (from `chrome-extension://`) are filtered out.
- **DOM nodes**: serialized as `{type: "node", name: "<nodeName>", tag: "<tagName>"}`.
- **Property accessors** on serialized objects: marked as `{type: "accessor"}` (the getter is not invoked during serialization, so wrapping a property does not cause recursive recording when the receiver is serialized).

## Wrap

A placed observation at a function or property you name. odda replaces the function or property descriptor via a userscript injected at `document_start`, and records each call or access with its receiver (`this`), arguments, return value, and call stack. Wraps are **leaf-only**: a wrap records the call it was placed on and does not follow callbacks passed as arguments. To see what a registered callback does, use Coverage to find the handler's code path and a Logpoint to read locals at the interesting line.

Tools:

- `wrap_calls_add(browser_id, tab_id, name, expr)` — Install a wrap on a function (e.g. `JSON.parse`, `EventTarget.prototype.addEventListener`). The wrapper calls through to the original and pushes a record with `{wrap, type: "call", this, args, ret, stack}`. Takes effect on the next navigation (re-navigate the tab or open a new one).
- `wrap_access_add(browser_id, tab_id, name, expr)` — Install a wrap on a property accessor (e.g. `HTMLElement.prototype.innerHTML`, `document.cookie`). Both getter and setter are wrapped if present. A get records `ret` as the value read; a set records `args[0]` as the value written with `ret: null`.
- `wrap_list(browser_id, tab_id)` — List installed wraps as `[{name, type, expr}]`.
- `wrap_remove(browser_id, tab_id, name)` — Remove a wrap's userscript from the browser's scope and reload its extension. Stops recording on future navigations. Records already captured in the current page are not affected.
- `wrap_dump(browser_id, tab_id, name=None)` — Read the per-tab wrap record array. Returns `[{wrap, type, this, args, ret, stack, error?}]`. Pass `name` to filter server-side to one wrap's records (useful for narrowing context when several wraps are installed).
- `wrap_clear(browser_id, tab_id)` — Zero the per-tab wrap record array without navigating. Returns `{status: "cleared", count: <records dropped>}`. Wrap installations are unaffected; subsequent calls continue to record.

### Wrap record shape

```json
{
  "wrap": "<name>",
  "type": "call" | "access",
  "this": "<serialized receiver>",
  "args": ["<serialized values>"],
  "ret": "<serialized return value, or null for setters>",
  "stack": [{"fn": "string|null", "url": "string|null", "line": "int|null", "col": "int|null"}],
  "error": "string (only present if the wrapped call threw)"
}
```

### Wrap lifecycle (distinctive)

- **Takes effect on next navigation.** `wrap_calls_add`/`wrap_access_add` install the wrapper, but already-loaded tabs are not re-injected. Re-navigate an existing tab (or open a new one) for the wrap to run.
- **Installations persist across navigation.** The wrap re-installs on every page load until you `wrap_remove` it.

## Logpoint

A placed observation at a source location you identify by script URL, line, and column. odda plants a non-pausing breakpoint whose condition evaluates an expression you supply, in the paused-then-immediately-resumed frame's scope. The page never stops. The expression can have side effects if you write them, but the intent is to read, not write.

You supply `url` (script URL), `line` (0-based), `col` (0-based), and `expr` (JS expression). The expression is evaluated in the paused frame's scope, so it can read locals by name. Minified code packs many statements per line, so **the column is required** to hit the right statement — without it, the logpoint binds to the first breakable location at or after the line, which may be a different statement than the one you want.

Tools:

- `logpoint_add(browser_id, tab_id, url, line, col, expr)` — Plant a logpoint. Returns `{"status": "planted", "id": "lp-<n>", "url", "line", "col", "expr"}` and optionally `"warning"` if no loaded script matches `url`.
- `logpoint_list(browser_id, tab_id)` — List planted logpoints as `[{id, url, line, col, expr}]`.
- `logpoint_dump(browser_id, tab_id)` — Read the per-tab logpoint record array. Returns `[{logpoint, url, line, col, value, error}]`.
- `logpoint_clear(browser_id, tab_id)` — Zero the per-tab logpoint record array without navigating. Returns `{status: "cleared", count: <records dropped>}`. Logpoint installations are unaffected.
- `logpoint_remove(browser_id, tab_id, lp_id)` — Remove a logpoint. If other logpoints share the same location, their combined breakpoint is rebuilt with the remaining expressions.

### Logpoint record shape

```json
{
  "logpoint": "lp-1",
  "url": "<script url>",
  "line": 87,
  "col": 12,
  "value": "<result of your expression, serialized>",
  "error": "string|null"
}
```

`value` is serialized via the shared serializer above. `error` is `null` on success, or the error message (e.g. `"ReferenceError: noSuchLocal is not defined"`) if the expression threw. A wrong local name produces an error record rather than silently recording nothing.

### Logpoint lifecycle (distinctive)

- **Persist until removed (not fire-once).** Logpoints keep recording across triggers within one page load. The installation persists across navigation too (it re-binds to the re-loaded script).
- **Per-tab-session (not durable).** Logpoints do not survive tab close. Re-plant after reopening a tab.
- **Stale-URL warning.** If no loaded script matches `url` at install time, the call succeeds but includes a `warning` field. The logpoint will not record until a script at that URL is loaded (e.g. after navigating to a page that loads it).

## Coverage

An aggregate query over a browsing context — start it, do the thing, stop it, read back per-block hit counts. Not placed at any target; records counts of executed blocks, not events. Use it when you know neither the function nor the line and need to find the code path that ran.

Tools:

- `coverage_start(browser_id, tab_id)` — Enable block-level coverage with per-block call counts and mark the tab as recording. Returns `{"status": "recording"}`. Starting on one tab does not affect another tab's recording. Calling it on a tab that is already recording errors so you know the previous recording is still live.
- `coverage_snapshot(browser_id, tab_id)` — Read per-script, per-block hit counts without stopping. Returns the coverage object. Zero-hit blocks are included (the negative space is as informative as the positive). The tab stays recording.
- `coverage_stop(browser_id, tab_id)` — Take a final coverage snapshot, stop recording, and return the same per-script, per-block output as `coverage_snapshot`.

### Coverage output shape

```json
{
  "scripts": [
    {
      "url": "<script url or null>",
      "functions": [
        {
          "name": "<function name or null>",
          "ranges": [{"startOffset": 0, "endOffset": 42, "count": 1}]
        }
      ]
    }
  ]
}
```

`url` is the script's URL, or `null` for inline scripts or scripts odda could not resolve. Each `range` is a block; `count` is the number of times that block executed within the take window. Zero-hit blocks appear with `count: 0`.

### Delta and cumulative semantics (important)

Each `coverage_snapshot` read resets the block counters, so a take returns the delta since the previous take (not a running total). odda accumulates these deltas server-side so:

- `coverage_snapshot` returns the **delta** since the previous take (or since `coverage_start` for the first take). Use it to read mid-window progress or to slice a sub-window (subtract two snapshot deltas).
- `coverage_stop` returns the **cumulative counts for the whole recording window** (the sum of every take since `coverage_start`, including any intermediate snapshot reads). You always get the full-window picture at stop, regardless of whether you snapshotted mid-way.

So the workflow is: `coverage_start` → trigger → (optional `coverage_snapshot` to peek) → trigger more → `coverage_stop` for the full window. To slice a sub-window, take a `coverage_snapshot` at the boundary, take another (or `coverage_stop`) later, and subtract.

### Coverage lifecycle and scope (distinctive)

- **Per-tab.** Coverage state is per-tab: starting on one tab does not affect another. Tabs in the same browser session are independent recordings.
- **Navigation-persistent.** The recording survives main-frame navigation. The recording window is `[coverage_start, coverage_stop]` regardless of how many navigations happen inside it. This enables the primary workflow: `coverage_start` → `navigate` (to trigger the behavior under investigation) → `coverage_snapshot`/`coverage_stop`.
- **Counts merge across loads.** Counts for the same script URL sum across navigations; different URLs get separate entries. To slice per-load, `coverage_snapshot` before the navigate and `coverage_snapshot` after, and subtract.
- **Does not survive tab close.** Closing a tab clears its coverage state.
- **Scope.** Main frame and same-origin iframes only. Cross-origin iframes and worker contexts are out of scope.