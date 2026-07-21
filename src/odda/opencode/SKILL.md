---
name: odda
description: Browser automation and HTTP traffic capture via the odda CLI
---

`odda` is a CLI tool for browser automation and HTTP traffic capture. The OpenCode plugin starts the odda server automatically when the session begins and exposes `ODDA_SOCKET` and `ODDA_DATA_DIR` environment variables for all shell tools.

You almost never need to run `odda server` yourself. Prefer the commands below.

All commands output JSON by default. Errors are returned as JSON with a non-zero exit code. Global options `--socket` and `--data-dir` are set by the plugin via environment variables, so they can normally be omitted.

## Quick command reference

| What you want to do         | Command                                                            |
| --------------------------- | ----------------------------------------------------------------- |
| Check the server is running | `odda status`                                                      |
| Get the HTTP proxy URL      | `odda proxy-url`                                                   |
| Open a Chrome window        | `odda browser open [--headless]`                                  |
| Close a browser             | `odda browser close <id>`                                          |
| List tabs (overview)        | `odda tabs list [--browser-id <id>]`                              |
| Open a new tab              | `odda tabs open --browser-id <id> [--url <url>]`                   |
| Close a tab                 | `odda tabs close --browser-id <id> --tab-id <n>`                   |
| Navigate an existing tab    | `odda navigate <url> --browser-id <id> --tab-id <n>`               |
| Run JavaScript              | `odda eval "<js>" --browser-id <id> --tab-id <n>` or `--file <path>` |
| Wait for a JS condition     | `odda wait-for "<expr>" --browser-id <id> --tab-id <n> [--timeout N]` |
| Screenshot                  | `odda screenshot --browser-id <id> --tab-id <n>`                   |
| List event listeners        | `odda event-listeners --browser-id <id> --tab-id <n>`              |
| Install a userscript        | `odda userscript install --name <name> --browser-id <id> --file <path>` |
| List userscripts            | `odda userscript list`                                             |
| Remove a userscript         | `odda userscript remove <name> --browser-id <id>`                  |
| Start block coverage        | `odda coverage start --browser-id <id> --tab-id <n>`               |
| Read coverage mid-recording | `odda coverage snapshot --browser-id <id> --tab-id <n>`             |
| Stop coverage + final counts| `odda coverage stop --browser-id <id> --tab-id <n>`                |
| Wrap a function (calls)     | `odda wrap calls add --browser-id <id> --tab-id <n> --expr "<js>" --name <name>` |
| Wrap a property accessor    | `odda wrap access add --browser-id <id> --tab-id <n> --expr "<js>" --name <name>` |
| List installed wraps        | `odda wrap list --browser-id <id> --tab-id <n>`                    |
| Remove a wrap               | `odda wrap remove --browser-id <id> --tab-id <n> <name>`           |
| Dump wrap records           | `odda wrap dump --browser-id <id> --tab-id <n>`                    |
| Clear wrap records          | `odda wrap clear --browser-id <id> --tab-id <n>`                   |
| Plant a logpoint            | `odda logpoint add --browser-id <id> --tab-id <n> --url <url> --line <n> --col <n> --expr "<js>"` |
| List planted logpoints      | `odda logpoint list --browser-id <id> --tab-id <n>`                 |
| Dump logpoint records       | `odda logpoint dump --browser-id <id> --tab-id <n>`                 |
| Clear logpoint records      | `odda logpoint clear --browser-id <id> --tab-id <n>`                |
| Remove a logpoint           | `odda logpoint remove --browser-id <id> --tab-id <n> --id <lp-id>` |
| Read server logs            | `odda logs [--follow] [--n N]`                                     |
| Clone a flow to edit        | `odda request clone <flow-id> --name <name> [--force]`             |
| Create an empty request     | `odda request new --name <name> [--force]`                         |
| Send an editable request    | `odda request send <name> [flags]`                                 |

## Targeting model

Every browser/tab command takes an explicit target. Agents always specify which browser and which tab they mean.

- **Browser-scoped commands** take `--browser-id`: `browser open`, `browser close`, `tabs list` (optional filter), `tabs open`, `userscript install/remove`.
- **Tab-scoped commands** take both `--browser-id` and `--tab-id`: `navigate` (existing tab), `eval`, `wait-for`, `screenshot`, `event-listeners`, `tabs close`, `coverage start`/`snapshot`/`stop`, `wrap calls add`/`access add`/`list`/`remove`/`dump`/`clear`, `logpoint add`/`list`/`remove`/`dump`/`clear`.

IDs are integers, monotonic, and **never reused**. A closed tab's id is retired forever; a stale `--tab-id` errors cleanly instead of silently hitting a different tab. This makes it safe for multiple agents to share one odda server: each agent owns the IDs it captured and never disturbs another agent's target.

Get IDs once and reuse them:
- `odda browser open` returns `{browser_id, tab_id, status}` — the initial tab is ready to use immediately.
- `odda tabs open --browser-id B [--url U]` returns `{browser_id, tab_id, status}` — the new tab_id.
- `odda tabs list` returns `[{browser_id, tabs: [{tab_id, url, title}]}]`.

Errors surface as JSON `{error: ...}` with a **non-zero exit code**: unknown `--browser-id`, unknown/mismatched `--tab-id`, navigation failure, screenshot failure, and "tab closed during operation" (another agent closed the tab mid-op) all raise. JS-execution errors (script throws) still return as strings inside a successful response, and `wait-for` timeout still raises a timeout error.

## Browser commands

- `odda browser open [--headless]` — Open a new Chrome window. Returns `{browser_id, tab_id, status}`. The `tab_id` is the initial tab; you can navigate/eval it immediately. `--headless` runs Chrome without a visible window (useful for CI and automated testing).
- `odda browser close <id>` — Close a browser instance by ID. Returns `{browser_id, status}`. Tearing down is immediate; any in-flight tab ops on that browser error cleanly with "tab closed during operation". For an overview of all browsers and their tabs use `odda tabs list` (no `--browser-id`); `odda status` reports the open browser count.

## Tab commands

- `odda tabs list [--browser-id <id>]` — List tabs grouped by browser as `[{browser_id, tabs: [{tab_id, url, title}]}]`. Without `--browser-id`, lists every open browser (a browser with zero tabs appears with `tabs: []`). With `--browser-id`, lists one browser's tabs.
- `odda tabs open --browser-id <id> [--url <url>]` — Open a new tab in the target browser. Without `--url` the tab opens at `about:blank`. Returns `{browser_id, tab_id, status}`.
- `odda tabs close --browser-id <id> --tab-id <n>` — Close an individual tab. Returns `{browser_id, tab_id, status}`. Closing the **last** tab leaves the browser open with zero tabs (matching Chrome's behavior); the browser can still accept `tabs open` later. To close the whole browser use `odda browser close`.

Tab ids are per-browser, monotonic, and never reused. After a tab closes, its id is gone; a later `tabs open` gets a strictly higher id.

## Navigation and page interaction

- `odda navigate <url> --browser-id <id> --tab-id <n>` — Navigate an existing tab to a URL. Returns `{status}`. To open a tab, use `odda tabs open`. Navigation failures (network error, invalid URL) raise a JSON error with non-zero exit.
- `odda eval "<js>" --browser-id <id> --tab-id <n>` — Execute JavaScript in the target tab and return the result. Returned Promises are awaited automatically: `fetch(url).then(r => r.status)` returns `200`, not a Promise object. Return a serializable value from async expressions — bare `fetch(url)` returns `{}` because the resolved `Response` is not JSON-serializable; chain `.then(r => r.text())` or similar to extract a serializable value. `odda eval --file <path> --browser-id <id> --tab-id <n>` loads JavaScript from a file (mutually exclusive with the inline argument; useful for multi-line scripts and shell-escape avoidance).
- `odda wait-for "<expr>" --browser-id <id> --tab-id <n> [--timeout N]` — Poll a JS expression until it's truthy or the timeout (default 30s) is reached. Uses Playwright's `wait_for_function`, which polls in-browser with no round-trips. Runs in the main world, so it sees page globals and userscript-injected helpers. Returns the truthy value on success; errors with non-zero exit code on timeout. Example: `odda wait-for "document.querySelector('.sdk-ready')" --browser-id 1 --tab-id 1 --timeout 10`.
- `odda screenshot --browser-id <id> --tab-id <n>` — Capture a JPEG screenshot of the target tab's viewport. Returns the path to the temp file (a bare string). Screenshot failures raise a JSON error with non-zero exit.
- `odda event-listeners --browser-id <id> --tab-id <n>` — List JavaScript event listeners attached to `window` and `document` in the target tab.

## Userscripts

`odda userscript` manages JavaScript helpers that auto-run at `document_start` on every navigation. Install a helper once and it runs before the page's own scripts on every `odda navigate` and `odda tabs open`.

Userscripts are stored on disk under `.odda/userscripts/<name>/script.js`. A Chrome extension is generated at `.odda/userscripts-extension/` with a `content.js` that inlines all installed userscripts (each wrapped in try/catch). The extension is loaded via CDP `Extensions.loadUnpacked` when a browser is opened.

odda also ships built-in default userscripts that are always injected before any installed userscripts. Currently this includes a dialog interceptor that records calls to `window.print`, `window.alert`, `window.confirm`, and `window.prompt` in `window.__oddaDialogs`.

Commands:

- `odda userscript install --name <name> --browser-id <id> --file <path>` — Install a JS file as a userscript. Overwrites any existing userscript of the same name and reloads the extension on the given browser. Alternatively, use `--source "<js>"` for inline source (mutually exclusive with `--file`).
- `odda userscript list` — List installed userscripts with their names and sizes. (Global — lists files on disk, not per-browser state.)
- `odda userscript remove <name> --browser-id <id>` — Remove a userscript from disk and reload the extension on the given browser. The script's effects on the current page are not undone; it won't run on future navigations.

Behavior notes:

- **Before page scripts.** Userscripts run at `document_start`, so `window` modifications are visible to the page before any of its own scripts execute. This is the key advantage over `odda eval` (which runs after navigation).
- **All tabs and frames.** The extension's content script matches `<all_urls>` and runs in all frames (`all_frames: true`), applying to every tab in every browser.
- **Idempotent re-injection.** Scripts run on every navigation. Write them to be idempotent (e.g., guard with `if (window.__myHelper__) return;`).
- **Reload applies on next navigation.** `install`/`remove` reload the extension for the given browser, but already-loaded tabs are not re-injected. Re-navigate an existing tab (or open a new one) for the change to take effect there.
- **Dialog interceptor.** `odda eval "window.__oddaDialogs" --browser-id <id> --tab-id <n>` returns an array of captured dialog/print events. Each entry has `{type, url, timestamp, stack, result?}` plus `message` and `defaultValue` when applicable. `type` is one of `print`, `alert`, `confirm`, `prompt`. `message` is present for `alert`/`confirm`/`prompt`. `defaultValue` is present for `prompt`. `result` is recorded for `confirm`/`prompt`. Use this to inspect what modal dialogs or print calls a page triggered during automation.

## Coverage

`odda coverage` records which code blocks execute during a window of interest. It is an **aggregate query** — you start it, do the thing, then read back per-block hit counts. Not placed at any target; records counts, not events. Use it when you know neither the function nor the line and need to find the code path that ran.

Commands (all tab-scoped — take `--browser-id` and `--tab-id`):

- `odda coverage start --browser-id <id> --tab-id <n>` — Enable block-level coverage with per-block call counts and mark the tab as recording. Returns `{"status": "recording"}`. Starting on one tab does not affect another tab's recording.
- `odda coverage snapshot --browser-id <id> --tab-id <n>` — Read per-script, per-block hit counts without stopping. Returns the coverage object. Zero-hit blocks are included (the negative space is as informative as the positive). The tab stays recording.
- `odda coverage stop --browser-id <id> --tab-id <n>` — Take a final coverage snapshot, stop recording, and return the same per-script, per-block output as `snapshot`.

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

Each `snapshot` read resets V8's block counters, so a take returns the delta since the previous take (not a running total). odda accumulates these deltas server-side so:

- `snapshot` returns the **delta** since the previous take (or since `start` for the first take). Use it to read mid-window progress or to slice a sub-window (subtract two `snapshot` deltas).
- `stop` returns the **cumulative counts for the whole recording window** (the sum of every take since `start`, including any intermediate `snapshot` reads). You always get the full-window picture at `stop`, regardless of whether you snapshotted mid-way.

So the workflow is: `start` → trigger → (optional `snapshot` to peek) → trigger more → `stop` for the full window. To slice a sub-window, take a `snapshot` at the boundary, take another (or `stop`) later, and subtract.

### Lifecycle and scope

- **Per-tab.** Coverage state is per-tab: starting on one tab does not affect another. Multiple agents sharing one odda server do not disturb each other's recordings.
- **Navigation-persistent.** The recording survives main-frame navigation. The recording window is `[start, stop]` regardless of how many navigations happen inside it. This enables the primary workflow: `start` → `navigate` (to trigger the behavior under investigation) → `snapshot`/`stop`.
- **Counts merge across loads.** Counts for the same script URL sum across navigations; different URLs get separate entries. To slice per-load, `snapshot` before the navigate and `snapshot` after, and subtract.
- **Does not survive tab close.** Closing a tab clears its coverage state.
- **Scope.** Main frame and same-origin iframes only. Cross-origin iframes and worker contexts are out of scope.
- **Errors.** Commands on a missing or closed tab, a missing browser, or a tab that is not recording return `{"error": ...}` with a non-zero exit code. Calling `start` on a tab that is already recording errors so you know the previous recording is still live.

## Wrap

`odda wrap` installs a transparent wrapper at a named function or property accessor. The wrapper runs before the page's own scripts on every navigation. Each call or access is recorded with its receiver, arguments, return value, and call stack. Use it when you know the function or property and want to see what flows through it.

Wraps are **leaf-only**: a wrap records the call it was placed on and does not follow callbacks passed as arguments. To see what a registered callback does, use Coverage to find the handler's code path and a Logpoint to read locals at the interesting line.

Commands (all tab-scoped — take `--browser-id` and `--tab-id`):

- `odda wrap calls add --browser-id <id> --tab-id <n> --expr "<js>" --name <name>` — Install a wrap on a function (e.g. `JSON.parse`, `EventTarget.prototype.addEventListener`). The wrapper calls through to the original and pushes a record with `{wrap, type: "call", this, args, ret, stack}`. The wrap takes effect on the next navigation (re-navigate the tab or open a new one).
- `odda wrap access add --browser-id <id> --tab-id <n> --expr "<js>" --name <name>` — Install a wrap on a property accessor (e.g. `HTMLElement.prototype.innerHTML`, `document.cookie`). Both getter and setter are wrapped if present. A get records `ret` as the value read; a set records `args[0]` as the value written with `ret: null`.
- `odda wrap list --browser-id <id> --tab-id <n>` — List installed wraps as `[{name, type, expr}]`. Wraps are stored on disk as named userscripts and apply to all tabs; the `--tab-id` is validated for targeting consistency but does not filter the list.
- `odda wrap remove --browser-id <id> --tab-id <n> <name>` — Remove a wrap's userscript and reload the extension. The wrap stops recording on future navigations. Records already captured in the current page are not affected.
- `odda wrap dump --browser-id <id> --tab-id <n>` — Read the per-tab wrap record array. Returns `[{wrap, type, this, args, ret, stack, error?}]`. The agent does not need to know the internal array name.
- `odda wrap clear --browser-id <id> --tab-id <n>` — Zero the per-tab wrap record array without navigating. Returns `{status: "cleared", count: <records dropped>}`. Wrap installations are unaffected; subsequent calls continue to record.

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

### Serialization rules

- **Functions** (in `args`, `ret`, or `this`): serialized as `{type: "function", name: "<inferred name or null>"}`. The function body is not captured; you cannot invoke captured functions.
- **Large or cyclic values**: truncated and marked. Objects with more than 50 keys become `{type: "object", truncated: true, keys: [...]}`. Arrays longer than 100 become `{type: "array", truncated: true, length: N}`. Strings longer than 10000 characters become `{type: "string", truncated: true, length: N, preview: "..."}`. Cycles become `{type: "object", truncated: true, cycle: true}`. Depth beyond 5 levels is truncated.
- **Call stack frames**: one shape `{fn, url, line, col}`; native or eval frames leave `url`/`line`/`col` null. Extension frames (from `chrome-extension://`) are filtered out.
- **DOM nodes**: serialized as `{type: "node", name: "<nodeName>", tag: "<tagName>"}`.
- **Property accessors** on serialized objects: marked as `{type: "accessor"}` (the getter is not invoked during serialization, so wrapping a property does not cause recursive recording when the receiver is serialized).

### Lifecycle and scope

- **Takes effect on next navigation.** `wrap calls add`/`access add` install the wrapper, but already-loaded tabs are not re-injected. Re-navigate an existing tab (or open a new one) for the wrap to run.
- **Records wipe on navigation.** Records from the previous page load are gone after a navigate. **Dump before navigating again** or the records are lost.
- **Installations persist across navigation.** The wrap re-installs on every page load until you `wrap remove` it.
- **Scope: all frames.** Wraps reach all frames in the tab including cross-origin iframes. Same-origin iframes aggregate records to the top frame (so `wrap dump` on the main tab sees them); cross-origin iframes keep their own records (the wrap still runs there, but the records stay in the iframe's context — read them by evaluating in the iframe). Wraps do not reach worker contexts.
- **Errors.** Commands on a missing or closed tab or a missing browser return `{"error": ...}` with a non-zero exit code. `wrap remove` on a non-existent wrap name errors.

## Logpoint

`odda logpoint` plants a non-pausing observation at a source location (script URL, line, column). The page never pauses — odda evaluates the agent-supplied expression in the paused-then-immediately-resumed frame's scope, records the result, and resumes. Use it when you know the line.

The agent supplies `--url` (script URL), `--line` (0-based), `--col` (0-based), and `--expr` (JS expression). The expression is evaluated in the paused frame's scope, so it can read locals by name. Minified code packs many statements per line, so **the column is required** to hit the right statement.

Commands (all tab-scoped — take `--browser-id` and `--tab-id`):

- `odda logpoint add --browser-id <id> --tab-id <n> --url <url> --line <n> --col <n> --expr "<js>"` — Plant a logpoint. Returns `{"status": "planted", "id": "lp-<n>", "url", "line", "col", "expr"}` and optionally `"warning"` if no loaded script matches `--url`.
- `odda logpoint list --browser-id <id> --tab-id <n>` — List planted logpoints as `[{id, url, line, col, expr}]`.
- `odda logpoint dump --browser-id <id> --tab-id <n>` — Read the per-tab logpoint record array. Returns `[{logpoint, url, line, col, value, error}]`.
- `odda logpoint clear --browser-id <id> --tab-id <n>` — Zero the per-tab logpoint record array without navigating. Returns `{status: "cleared", count: <records dropped>}`. Logpoint installations are unaffected.
- `odda logpoint remove --browser-id <id> --tab-id <n> --id <lp-id>` — Remove a logpoint. The logpoint stops recording on future hits. If other logpoints share the same location, their combined breakpoint is rebuilt with the remaining expressions.

### Logpoint record shape

```json
{
  "logpoint": "lp-1",
  "url": "<script url>",
  "line": 87,
  "col": 12,
  "value": "<result of the agent's expression, serialized>",
  "error": "string|null"
}
```

`value` is serialized via the same serializer as Wrap records (opaque function refs, truncation for large/cyclic values — see the Wrap serialization rules). `error` is `null` on success, or the error message (e.g. `"ReferenceError: noSuchLocal is not defined"`) if the expression threw. A wrong local name produces an error record rather than silently recording nothing.

### Lifecycle and scope

- **Persist until removed (not fire-once).** Logpoints keep recording across triggers within one page load. The installation persists across navigation too (it re-binds to the re-loaded script).
- **Records wipe on navigation.** Records from the previous page load are gone after a navigate. **Dump before navigating again** or the records are lost. Installations are unaffected by navigation.
- **Per-tab-session (not durable).** Logpoints do not survive tab close. Re-plant after reopening a tab.
- **Column is required.** `--col` is not optional. Minified code packs many statements per line; without the column, the logpoint binds to the first breakable location at or after the line, which may be a different statement than the one you want.
- **Stale-URL warning.** If no loaded script matches `--url` at install time, the command succeeds but includes a `warning` field. The logpoint will not record until a script at that URL is loaded (e.g. after navigating to a page that loads it).
- **Scope: main frame and same-origin iframes only.** Cross-origin iframes and worker contexts are out of scope. (Wraps reach cross-origin iframes; Logpoints do not.)
- **Errors.** Commands on a missing or closed tab or a missing browser return `{"error": ...}` with a non-zero exit code. `logpoint remove` on an unknown logpoint id errors.

## Recipe: postMessage origin-validation investigation

The three dynamic-analysis concepts compose. The canonical investigation is confirming a `postMessage` handler validates the origin of incoming messages:

1. **Wrap** `EventTarget.prototype.addEventListener` to confirm a `message` handler is registered and capture the handler reference:
   ```
   odda wrap calls add --browser-id 1 --tab-id 1 --expr EventTarget.prototype.addEventListener --name ael
   odda navigate http://target/ --browser-id 1 --tab-id 1   # re-navigate so the wrap runs
   odda eval "String(window.__oddaWrapFixture(window, 'message', function onMsg() {}))" --browser-id 1 --tab-id 1
   odda wrap dump --browser-id 1 --tab-id 1   # confirm 'message' registration, capture handler ref
   ```
2. **Coverage** to find which code path the handler runs when a message arrives:
   ```
   odda coverage start --browser-id 1 --tab-id 1
   odda eval "window.postMessage({type: 'probe'}, '*')" --browser-id 1 --tab-id 1
   odda coverage stop --browser-id 1 --tab-id 1   # find the script URL + block ranges that ran
   ```
3. Read the handler source from the captured flow body (the script URL from coverage maps to a flow in `.odda/flows/`) to find the origin-check line.
4. **Logpoint** at that line to read the locals the check operates on:
   ```
   odda logpoint add --browser-id 1 --tab-id 1 --url <script-url> --line <n> --col <n> --expr "event.origin"
   odda eval "window.postMessage({type: 'probe'}, 'https://evil/')" --browser-id 1 --tab-id 1
   odda logpoint dump --browser-id 1 --tab-id 1   # read the captured origin value
   ```
5. If the logpoint records an attacker-controllable origin, the handler does not validate origin and is vulnerable.

The workflow: Wrap confirms the API touch, Coverage finds the code path, Logpoint reads the locals at the interesting line. Each concept observes without modifying behavior (the page never pauses).

## Raw request commands

`odda request` lets you craft and send raw HTTP requests byte-for-byte, bypassing the browser. Use it to replay/modify captured flows or send hand-built requests for header-injection, smuggling, and parser-differential tests.

Editable requests live in `.odda/requests/<name>/`:

- `request` — the raw HTTP request bytes (request line + headers + blank line + body), **CRLF-terminated**, same format as `.odda/flows/<id>/request`. Edit this file with the built-in edit tool. Ensure `\r\n` line endings (use `printf` or `sed 's/$/\r/'` when writing via shell — heredocs use `\n` which will fail on the wire).
- `meta.json` — sidecar with `{"scheme": "http"|"https", "host": "...", "port": N}`. `send` uses this to open the socket; the `request` file is origin-form and carries no scheme/port. The `host` here is the TCP destination — it may intentionally differ from the `Host` header in the request file (for vhost/host-header/SSRF tests).

Commands:

- `odda request clone <flow-id> --name <name> [--force]` — Copy `.odda/flows/<flow-id>/request` into `.odda/requests/<name>/request` and synthesize `meta.json` from the flow's `flows.jsonl` record (scheme/port) plus the `Host` header's explicit port. Refuses to overwrite an existing request unless `--force`.
- `odda request new --name <name> --host <host> [--protocol http|https] [--port <port>] [--force]` — Create an empty `request` file (0 bytes) and a `meta.json` with the given host, protocol (default `https`), and port (default 80 for `http`, 443 for `https`). Fill the `request` file with the edit tool.
- `odda request send <name> [--fix-content-length] [--timeout 30] [--insecure]` — Read both files, open a TCP socket (TLS for https, ALPN `h2` when the request line says `HTTP/2`), write the exact bytes from the `request` file, read the response, decode it (de-chunk + gzip/br/deflate/zstd), and write a flow record to `.odda/flows/<NNNNN>/`. The sent request is recorded before the network exchange (two-phase durability), so a crash leaves a durable request file. Output is the `flows.jsonl` record that was appended; read `.odda/flows/<id>/response_body.*` for the body.

Flags for `send`:

- `--fix-content-length` — Recompute `Content-Length` from the body and overwrite the header (the `request` file on disk is untouched). Use this when you've edited the body and want the framing auto-corrected. Skip it for Content-Length smuggling/differential tests where the wrong value is the point.
- `--timeout <seconds>` — Total timeout for connect + reads (default 30). On timeout, a flow record is written with whatever was received plus an `error` file.
- `--insecure` — Skip TLS certificate verification. Default verifies.

Behavior notes:

- **Single-shot, no redirects.** A 3xx response is recorded as-is; re-`send` manually if you want to follow.
- **No pre-flight validation.** Malformed requests fail at the socket/TLS/H2 layer; the error is captured in the flow's `error` file.
- **HTTP/2** — if the request line says `HTTP/2`, `send` negotiates ALPN `h2` and emits real H2 frames (HPACK-encoded pseudo-headers synthesized from the request line + `Host` + `meta.json`). The stored `request` file stays H1-shaped text with `HTTP/2` in the version field (consistent with how mitmproxy stores captured H2 flows). If the server doesn't negotiate `h2`, `send` errors — edit the request line to `HTTP/1.1` and resend.
- **Missing framing** — if a body exists with no `Content-Length` and no `Transfer-Encoding: chunked`, `send` half-closes the socket (`write_eof`) after the body so the server sees EOF.
- **Binary bodies** — the `request` file is bytes; populate it via shell (`cat`, `cp`) if the edit tool can't author the bytes you need.
- **Captured-sent requests are stored in the same `flows.jsonl`** as proxied captures, with `scheme` and `port` fields populated. `flows.jsonl` records from older captures may lack these fields; treat them as `https`/`443` when absent.

## Proxy and flow capture

- `odda proxy-url` — Return the HTTP proxy URL as plain text. Route HTTP clients through this URL to capture traffic.

Captured flows are stored as read-only files under `.odda/flows/`.

### Flow file layout

```
.odda/flows/
├── flows.jsonl                # append-only index, one JSON line per completed/errored flow
└── <NNNNN>/                   # zero-padded monotonic flow id (e.g. 00001)
    ├── request                # reconstructed HTTP request (request line + headers + blank line + decoded body), CRLF
    ├── meta.json              # read-only sidecar with {"scheme":"https","host":"...","port":443}
    ├── response_headers       # reconstructed status line + headers + blank line, CRLF (no body)
    ├── response_body.<ext>    # decoded response body, ext from Content-Type (e.g. .json, .html, .bin); omitted for excluded/empty bodies
    └── error                  # present only on errored flows (e.g. server unreachable)
```

### flows.jsonl schema

One JSON object per line, in completion order:

```json
{
  "id": "00042",
  "method": "GET",
  "scheme": "https",
  "host": "example.com",
  "port": 443,
  "path": "/",
  "status_code": 200,
  "total_duration_ms": 12.3,
  "body_file": "flows/00042/response_body.json",
  "error": null
}
```

- `id` — zero-padded flow id matching the directory name; lets you re-sort by capture order with `sort`.
- `scheme` / `port` — request scheme (`http`/`https`) and port. Populated for new captures and `odda request send` flows; absent on records written by older odda versions (treat as `https`/`443`).
- `status_code` — `null` for errored flows (the `error` field holds the message instead).
- `body_file` — path relative to `.odda`; read it as `read ".odda/$body_file"`. `null` when the body was excluded (images/video/audio/fonts) or empty.
- `error` — `null` for completed flows; the error message for failed flows.

### Note on response bodies

`response_body.<ext>` holds the **decoded** body (mitmproxy inflates gzip/br/deflate). The `response_headers` file shows the original on-wire headers, so `Content-Encoding: gzip` and the compressed `Content-Length` may not match the decoded body file. This is expected.

Per-flow files (`request`, `response_headers`, `response_body.*`, `error`) are written read-only (mode 0444) so history cannot be edited.

## Server commands

- `odda status` — Show server status, including socket path, data directory, proxy URL, and open browser count.
- `odda logs [--follow] [--n N]` — Show or tail the server log. Default `--n` is 50.
- `odda version` — Print the odda version.

`odda server` and `odda install-opencode` exist but are normally handled by the plugin and the package installer, not by agents at runtime.
