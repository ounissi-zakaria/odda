---
name: odda
description: Browser automation, HTTP traffic capture, dynamic analysis, and raw request crafting via the odda CLI. Use when driving Chrome, capturing flows, observing JS execution, or hand-building HTTP requests.
---

`odda` is a CLI tool for browser automation and HTTP traffic capture. The odda plugin starts the odda server automatically when the session begins and exposes `ODDA_SOCKET`, `ODDA_DATA_DIR`, and `ODDA_LOG` environment variables for all shell tools. You almost never need to run `odda server` yourself.

All commands print human-readable **text output** by default: tables for lists (`tabs list`, `wrap list`, `logpoint list`, `userscript list`, `event-listeners`), `key: value` lines for flat results (`status`, `browser open`, `request send`), YAML-ish blocks for nested records (`wrap dump`, `logpoint dump`, `coverage snapshot`/`stop`), and the bare value for scalars (`screenshot` path, `navigate` status, `eval` result). Errors print as `Error: <message>` on stderr with a non-zero exit code; stdout stays empty on error. Pass the global `--json` flag (before the subcommand: `odda --json <cmd> ...`) to emit **structured output** instead — the JSON form, stable by convention, which is the parse target for scripts and for structural queries on `wrap dump` / `logpoint dump` / `coverage snapshot` / `coverage stop` (whose nested records a flat text format cannot express). In `--json` mode, errors print as `{"error": ...}` on stdout. Global options `--socket` and `--data-dir` are set by the plugin via environment variables, so they can normally be omitted.

**Use text output by default** (it's readable and token-cheap); reach for `--json` only when you need to programmatically extract a value into a shell pipeline or assert structural properties of `wrap dump` / `logpoint dump` / `coverage snapshot` / `coverage stop` records.

## Targeting model

Every browser/tab command takes an explicit target. Agents always specify which browser and which tab they mean.

- **Browser-scoped** commands take `--browser-id`: `browser open`, `browser close`, `tabs list` (optional filter), `tabs open`, `userscript install`/`remove`.
- **Tab-scoped** commands take both `--browser-id` and `--tab-id`: `navigate` (existing tab), `eval`, `wait-for`, `screenshot`, `event-listeners`, `tabs close`, `coverage *`, `wrap *`, `logpoint *`, `page *`.

Targeting flags go **after** the subcommand: `odda <cmd> --browser-id <id> [--tab-id <n>] ...` (e.g. `odda page snapshot --browser-id 1 --tab-id 1`). This is unlike the global `--json` flag, which goes **before** the subcommand (`odda --json <cmd> ...`); `--browser-id`/`--tab-id` before the subcommand is rejected.

**Exception — `request send` is server-scoped.** `odda request send` opens its own socket to the target host and bypasses the browser entirely, so it takes **no targeting flags** — `--browser-id`/`--tab-id` are rejected (Typer's "No such option" error). For cookie-gated or in-page authenticated requests, use `odda eval` running `fetch(url, {...})` from the page instead; the browser carries the session cookie automatically.

IDs are integers, monotonic, and **never reused**. A closed tab's id is retired forever; a stale `--tab-id` errors cleanly instead of silently hitting a different tab. This makes it safe for multiple agents to share one odda server: each agent owns the IDs it captured and never disturbs another agent's target.

Get IDs once and reuse them:
- `odda browser open` prints `browser_id`, `tab_id`, and `status` lines (text) or `{browser_id, tab_id, status}` (`--json`) — the initial tab is ready to use immediately.
- `odda tabs open --browser-id B [--url U]` prints the same `browser_id`/`tab_id`/`status` lines.
- `odda tabs list` prints a table (text) or `[{browser_id, tabs: [{tab_id, url, title}]}]` (`--json`).

## Quick command reference

Scoped by section below: browser-scoped commands take `--browser-id`; tab-scoped commands take `--browser-id` + `--tab-id`. See the Targeting model above.

Add the targeting flags from the Targeting model above to every command below — `--browser-id <id>` for browser-scoped, `--browser-id <id> --tab-id <n>` for tab-scoped. Only the per-command flags are shown here.

| What you want to do         | Command                                                            |
| --------------------------- | ----------------------------------------------------------------- |
| Check the server is running | `odda status`                                                      |
| Get the HTTP proxy URL      | `odda proxy-url`                                                   |
| Open a Chrome window        | `odda browser open [--headed]`                                   |
| Close a browser             | `odda browser close`                                              |
| List open browsers          | `odda browser list`                                               |
| List tabs (overview)        | `odda tabs list`                                                  |
| Open a new tab              | `odda tabs open [--url <url>]`                                    |
| Close a tab                 | `odda tabs close`                                                 |
| Navigate an existing tab    | `odda navigate --url <url> [--timeout N] [--wait-until <event>]`  |
| Run JavaScript              | `odda eval --js "<js>"` or `--file <path>`                        |
| Wait for a JS condition     | `odda wait-for --expression "<expr>" [--timeout N]`               |
| Screenshot                  | `odda screenshot [--output <path>]`                               |
| List event listeners        | `odda event-listeners`                                            |
| Snapshot (find refs)        | `odda page snapshot`                                              |
| Click by ref                | `odda page click --ref <ref> [--timeout N]`                       |
| Click by coordinates        | `odda page click -x <n> -y <n>`                                   |
| Fill by ref                 | `odda page fill --ref <ref> (--value "<v>" | --file <path>) [--timeout N]` |
| Hover by ref                | `odda page hover --ref <ref> [--timeout N]`                       |
| Hover by coordinates        | `odda page hover -x <n> -y <n>`                                   |
| Upload files by ref         | `odda page upload --ref <ref> --file <path> [--file <path>...]`   |
| Install a userscript        | `odda userscript install --name <name> --file <path>`             |
| List userscripts            | `odda userscript list`                                            |
| Remove a userscript         | `odda userscript remove --name <name>`                            |
| Install a proxy-script      | `odda proxy-script install --name <name> --file <path> [--force]` |
| List proxy-scripts          | `odda proxy-script list`                                          |
| Remove a proxy-script       | `odda proxy-script remove --name <name>`                          |
| Start block coverage        | `odda coverage start`                                             |
| Read coverage mid-recording | `odda coverage snapshot`                                          |
| Stop coverage + final counts| `odda coverage stop`                                              |
| Wrap a function (calls)     | `odda wrap calls add --expr "<js>" --name <name>`                 |
| Wrap a property accessor    | `odda wrap access add --expr "<js>" --name <name>`                |
| List installed wraps        | `odda wrap list`                                                  |
| Remove a wrap               | `odda wrap remove --name <name>`                                  |
| Dump wrap records           | `odda wrap dump [--name <name>]`                                  |
| Clear wrap records          | `odda wrap clear`                                                 |
| Plant a logpoint            | `odda logpoint add --url <url> --line <n> --col <n> --expr "<js>"` |
| List planted logpoints      | `odda logpoint list`                                              |
| Dump logpoint records       | `odda logpoint dump`                                              |
| Clear logpoint records      | `odda logpoint clear`                                             |
| Remove a logpoint           | `odda logpoint remove --id <lp-id>`                               |
| Read server logs            | `odda logs [--follow] [--n N]`                                     |
| Clone a flow to edit        | `odda request clone --flow-id <flow-id> --name <name> [--force]`   |
| Create an empty request     | `odda request new --name <name> [--line-terminator <bytes>] [--force]` |
| Send an editable request    | `odda request send --name <name> [flags]`                          |
| Send N on one connection    | `odda request send --name <a> --name <b> ... [--pipelining]`        |
| Send N concurrent copies    | `odda request send --name <name> --repeat N [--fix-content-length]`|

## Browser automation

Drives Chrome: open/navigate, run JS, take screenshots, and interact with the page by snapshot+ref. All tab-scoped unless noted.

**All browser traffic is routed through odda's HTTP proxy and captured as flows.** Every request/response the page makes — navigations, XHR/fetch, sub-resources, `window.open` popups — is saved under `.odda/flows/<id>/` (request bytes, response body, headers, timing). Read the response body from `.odda/flows/<id>/response_body.*` (see [FLOWS.md](FLOWS.md)) instead of extracting it from the page via `eval`; read `.odda/flows/flows.jsonl` for the index. This means driving the browser *is* traffic capture — there's no separate "record" toggle.

- `odda browser open [--headed]` — Open a new Chrome window (headless by default). Prints `browser_id`/`tab_id`/`status` lines (text) or `{browser_id, tab_id, status}` (`--json`); the initial tab is ready immediately. `--headed` shows the Chrome window (debugging, interactive use); the default runs headless, suiting autonomous agents that don't need a visible window.
- `odda browser close` — Close a browser instance. Tearing down is immediate; any in-flight tab ops on that browser error cleanly. For an overview of all browsers and their tabs use `odda tabs list` (no `--browser-id`); `odda status` reports the open browser count.
- `odda browser list` — List every tracked browser with its tab count. Text output is a table (`browser_id  tab_count`); `--json` returns `[{browser_id, tab_count}]`. Closed browsers are removed from odda's tracked set, so this lists only browsers still in memory. `(no browsers)` prints when the set is empty.
- `odda tabs list [--browser-id <id>]` — List tabs grouped by browser. Text output is a table (`browser_id  tab_id  url  title`); `--json` returns `[{browser_id, tabs: [{tab_id, url, title}]}]`. Without `--browser-id`, lists every open browser (a browser with zero tabs appears with `tabs: []`); with it, lists one browser's tabs.
- `odda tabs open [--url <url>]` — Open a new tab in the target browser. Without `--url` the tab opens at `about:blank`. Prints `browser_id`/`tab_id`/`status` lines (text) or the same as JSON (`--json`).
- `odda tabs close` — Close an individual tab. Closing the **last** tab leaves the browser open with zero tabs (matching Chrome's behavior); the browser can still accept `tabs open` later. To close the whole browser use `odda browser close`.
- `odda navigate --url <url> [--timeout N] [--wait-until <event>]` — Navigate an existing tab to a URL. To open a tab, use `odda tabs open`. `--timeout` (default 30s) bounds the navigation wait; `--wait-until` (default `load`) selects the page lifecycle event to wait for: `commit`, `domcontentloaded`, `load`, or `networkidle`. For SPAs that render at `DOMContentLoaded` but defer `load` (slow sub-resources, JS-rendered apps), pass `--wait-until domcontentloaded` to return as soon as the DOM is ready instead of blocking on the 30s `load` timeout. Text output is the status line (e.g. `Navigated to: <url>`); navigation failures (network error, invalid URL, or lifecycle-event timeout) print `Error: ...` on stderr with non-zero exit (`--json` gives `{"error": ...}`); the timeout error names the `wait-until` event that failed and points at `odda wait-for` for SPAs whose `load` event never fires.
- `odda eval --js "<js>"` — Execute JavaScript in the target tab and return the result. Pass an expression, not a `return` statement (`return` is illegal at the top level — use an IIFE `(()=>{ ... })()` if you need statements). Returned Promises are awaited automatically: `fetch(url).then(r => r.status)` returns `200`, not a Promise object. Return a serializable value from async expressions — bare `fetch(url)` returns `{}` because the resolved `Response` is not JSON-serializable; chain `.then(r => r.text())` or similar. `--file <path>` loads JavaScript from a file (mutually exclusive with `--js`; useful for multi-line scripts and shell-escape avoidance). **`eval` has no `--timeout` option** (unlike `navigate`/`wait-for`/`page`/`click`/`fill`/`hover`/`upload`, which have per-command timeouts) — it runs until the JS expression resolves, so a hung expression blocks the call indefinitely. For long enumeration loops (100+ `fetch`es) or scripts that may hang, bound the duration with the outer shell timeout (e.g. `timeout 60 odda eval --js "..."`). **Text output renders the result readably: strings print without quotes, objects/arrays as compact JSON, scalars (`42`, `true`, `null`) bare.** Calling `JSON.stringify` inside your JS no longer double-encodes in text mode — `eval --js "JSON.stringify({a:1})"` prints `{"a":1}` directly. Use `--json` if you need the value as a JSON-typed value in a pipeline.
- `odda wait-for --expression "<expr>" [--timeout N]` — Poll a JS expression until it's truthy or the timeout (default 30s) is reached. Polling happens in-browser with no round-trips. Runs in the main world, so it sees page globals and userscript-injected helpers. **A thrown error inside the expression is treated as falsy and polling continues** — so `document.querySelector('#root').children.length` keeps polling while `#root` is still absent instead of crashing on the null deref. Prints the truthy value on success (readably, like `eval`); errors with non-zero exit code on timeout. Example: `odda wait-for --expression "document.querySelector('.sdk-ready')" --timeout 10`.
- `odda screenshot [--output <path>]` — Capture a JPEG screenshot of the target tab's viewport. Prints the path to the screenshot file (a bare line in text mode, a JSON-quoted string with `--json`). By default a temp file is generated; pass `--output <path>` to write to a path you choose (the parent directory is created).
- `odda event-listeners` — List JavaScript event listeners attached to `window` and `document` in the target tab. Text output is a table (`type  element_tag  script_url  line_number  column_number`); `--json` returns the list of objects.

### Page interaction (snapshot + ref, or viewport coordinates)

`odda page` drives the browser to trigger behavior — clicking, filling, hovering, uploading, and snapshotting the page to find targets. The workflow is: **snapshot** to discover element **refs**, then pass the ref to the action command. `click` and `hover` alternatively accept **viewport coordinates** (`-x`/`-y`, CSS pixels from the top-left of the viewport) for targets the a11y tree can't name — canvas, custom hit-testing, elements behind overlays, or pixel locations taken from a screenshot. This is the ref-driven alternative to `odda eval` with hand-written CSS selectors, which is brittle on minified SPAs. Page interaction is both standalone (browser automation) and composes with Dynamic analysis: snapshot to find the target, click/fill/upload to trigger behavior, then Wrap/Coverage/Logpoint to observe what happened.

A **ref** (`eN`, or `f<frameSeq>eN` inside an iframe) is a short-lived name for one element in a snapshot. The agent passes `eN` via `--ref` to `click`, `fill`, `hover`, and `upload`; odda resolves it back to the element when the action runs.

- **Valid as long as the element stays in the DOM.** If the element is removed (SPA content swap, navigation), the action errors cleanly. Re-snapshot to discover refs for new elements; existing refs continue to work without re-snapshotting.
- **Cross-iframe is transparent.** Refs inside iframes have the form `f<frameSeq>eN`; odda resolves them automatically, no special handling.
- **File inputs without an accessible name do not appear in the snapshot.** A nameless `<input type="file">` is omitted from the a11y tree. Give the input an `aria-label` via `eval`, re-snapshot, then upload by ref. File inputs render as `button` elements in the a11y tree after labeling — upload by that ref. Example: `odda eval --js "document.querySelector('input[type=file]').setAttribute('aria-label','upload')"` → re-snapshot → upload by ref.
- **Dialog interception.** `confirm`/`prompt` **proceed by default**: a built-in userscript intercepts `window.alert`/`confirm`/`prompt`/`print` and `confirm` returns `true`, `prompt` returns `"odda"` so the page proceeds instead of being silently denied. All calls are recorded in `window.__oddaDialogs` — inspect via `eval --js "window.__oddaDialogs"` (each entry's `result` shows what the interceptor returned). `__oddaDialogs` is **per-window**: a dialog fired inside a cross-origin iframe is recorded in the iframe's `window`, which the top frame can't read cross-origin — reading `window.__oddaDialogs` from the top frame returns `[]` and is not evidence the payload failed (see [USERSCRIPTS.md](USERSCRIPTS.md) for the same-origin workaround). To supply a specific answer (e.g. a `prompt("Answer:")` expecting `s3cr3t`), pre-register it before the click: `odda eval --js "window.__oddaDialogResponses = {prompt: 's3cr3t'}"`, then `odda page click --ref e8` — the prompt returns `s3cr3t`. To restore deny-by-default per-type, register `{confirm: false, prompt: null}`. See [USERSCRIPTS.md](USERSCRIPTS.md) for the map semantics, verbatim passthrough, and the cross-frame/cross-navigation limitations.

Commands (all tab-scoped):
- `odda page snapshot` — Return the page's accessibility tree as YAML-ish text with `[ref=eN]` tags. The agent greps the text for the element it wants. No filter options; the full tree is returned. Prefer `page snapshot` over `odda screenshot` for finding elements and understanding page structure: the snapshot is text (cheaper on context, grep-able, carries refs for action commands), while the screenshot is pixels (useful only for visual layout, icons, or canvas the a11y tree can't see). Use screenshots when you need to see what the page looks like; use snapshots when you need to find an element to act on.
- `odda page click --ref <ref> [--timeout N]` — Click the element identified by `ref` (plain left-click). Prints `status: clicked` / `ref: <ref>` lines (text) or `{status: "clicked", ref: "<ref>"}` (`--json`). If the ref no longer resolves, errors cleanly with a stale-ref message.
- `odda page click -x <n> -y <n>` — Click at viewport-relative coordinates instead of by ref: a raw trusted mouse event with no element resolution, no actionability checks, and no timeout. The event lands on whatever renders at that point (iframes included); clicking empty space succeeds as a no-op. 
- `odda page fill --ref <ref> (--value "<value>" | --file <path>) [--timeout N]` — Fill the element identified by `ref` with `value`. Clears the field first, then types. Works on text inputs, textareas, contenteditable elements, checkboxes (`"true"`/`"false"`), radios, and selects. `--file <path>` reads the value from a file (preserves newlines; avoids shell-quoting pitfalls for multiline HTML payloads in textareas). `--value` and `--file` are mutually exclusive; at least one is required. Prints `status: filled` / `ref: <ref>` lines (text) or `{status: "filled", ref: "<ref>"}` (`--json`).
- `odda page hover --ref <ref> [--timeout N]` — Hover the element identified by `ref`. Auto-scrolls the element into view first. Prints `status: hovered` / `ref: <ref>` lines (text) or `{status: "hovered", ref: "<ref>"}` (`--json`).
- `odda page hover -x <n> -y <n>` — Hover at viewport-relative coordinates instead of by ref: the same raw trusted event dispatch as a coordinate click, without the button press. Whatever renders at that point (iframes included) receives the mouseover/mousemove/mouseenter cascade. No element resolution, no actionability checks, no scrolling, no timeout. `--ref` and `-x`/`-y` are mutually exclusive.
- `odda page upload --ref <ref> --file <path> [--file <path>...] [--timeout N]` — Set files on a file input identified by `ref`. Repeat `--file` for multiple files (`<input type="file" multiple>`). Prints `status: uploaded` / `ref: <ref>` plus a `files:` list (text) or `{status: "uploaded", ref: "<ref>", files: [...]}` (`--json`). **This sets the file on the input but does not submit the form** — click the form's submit button by ref separately to POST it.

Ref-mode actions (`--ref`) accept `--timeout` (default 5 seconds) for ref resolution and the action itself. This is shorter than `wait-for`'s 30s default because actions are interactive — the agent wants to know quickly when something didn't work. A stale ref errors within the timeout, not after a 30-second hang. Coordinate mode (`-x`/`-y`) has no timeout: there is no resolution or actionability wait, just the event dispatch.

## Userscripts (cross-cutting)

`odda userscript` manages JavaScript helpers that auto-run at `document_start` on every navigation, before the page's own scripts. Install a helper once and it runs before the page's own scripts on every `odda navigate` and `odda tabs open`, in the main world. Useful for both browser automation (inject helpers) and dynamic analysis (Wraps are userscripts; see [USERSCRIPTS.md](USERSCRIPTS.md)).

- `odda userscript install --name <name> --file <path>` — Install a JS file as a userscript into the given browser's scope. Overwrites any existing userscript of the same name and reloads that browser's extension. Alternatively, use `--source "<js>"` for inline source (mutually exclusive with `--file`). Prints `name`/`size`/`extension_id` lines (text) or the same as JSON (`--json`).
- `odda userscript list` — List installed userscripts for the given browser. Text output is a table (`name  size`); `--json` returns `[{name, size}]`.
- `odda userscript remove --name <name>` — Remove a userscript from the given browser's scope and reload its extension. The script's effects on the current page are not undone; it won't run on future navigations.

For per-browser storage internals, the built-in dialog interceptor (records `window.print`/`alert`/`confirm`/`prompt` calls into `window.__oddaDialogs`), and default userscripts, see [USERSCRIPTS.md](USERSCRIPTS.md).

## Proxy-scripts (cross-cutting, proxy layer)

`odda proxy-script` manages Python files in mitmproxy `-s` script format that odda adds to the running proxy's addon chain. Install one to intercept, modify, record, or generate HTTP traffic at the proxy layer — before it reaches the browser or the upstream server. A file is a valid proxy-script if `mitmproxy -s <file>` accepts it: top-level `request`/`response`/`load`/`running`/... hook functions, or an `addons = [...]` list for composition. **A proxy-script runs with full privileges** (file/network/subprocess access) — the same trust boundary as any shell command an agent runs; the risk is documented, not gated. See [PROXY-SCRIPTS.md](PROXY-SCRIPTS.md) for the format, scope, and failure model.

- `odda proxy-script install --name <name> --file <path>` — Install a `.py` file as a proxy-script. Refuses to overwrite an existing name without `--force` (same semantics as `request clone`/`new`); with `--force`, removes the existing instance and adds the new one. Alternatively, use `--source "<py>"` for inline source (mutually exclusive with `--file`). Prints `name`/`size` lines (text) or the same as JSON (`--json`). Survives an odda server restart.
- `odda proxy-script list` — List installed proxy-scripts. Text output is a table (`name  size`); `--json` returns `[{name, size}]`. A proxy-script that failed to load at boot is listed (on disk) but not live — check `odda logs` for the error.
- `odda proxy-script remove --name <name>` — Remove a proxy-script: deletes its on-disk source and removes the live addon from the proxy chain. Prints `name`/`removed` lines (text) or the same as JSON (`--json`).

Scope is **global** — one proxy shared across all browsers, so a proxy-script sees every flow (not per-browser like userscripts). Captured `.odda/flows/<id>/` files record the **original** request/response; a proxy-script's mutations affect what goes upstream, not what is captured. Runtime hook errors surface via `odda logs`; a failing proxy-script never crashes the proxy.

## Dynamic analysis

Observing JavaScript execution in progress — recording what code does as it runs, with the intent to observe rather than modify. Three peer concepts, chosen by what you know:

- **Wrap** — "I know the function/property" → wrap it and record each call/access.
- **Logpoint** — "I know the line" → plant a non-pausing observation at a source location.
- **Coverage** — "I know neither" → record which code blocks execute, then find the path.

**Common intents, mapped to the right concept** (reach for these before writing a custom userscript to wrap/observe JS):

- **"I want to intercept/log a DOM event or API call"** (e.g. `postMessage` handlers, `JSON.parse`, `addEventListener`) → **Wrap** (`odda wrap calls add --expr EventTarget.prototype.addEventListener` or the specific function). This replaces hand-writing a userscript to hook a function; the wrap records each call with `this`/`args`/`ret`/`stack` and takes effect on the next navigation.
- **"I want to trace untrusted data from a DOM source to where it's checked or sinks"** (e.g. a `postMessage` handler → `innerHTML`, `location.hash` → a sink) → the worked recipe at [recipes/dom-data-flow-tracing.md](recipes/dom-data-flow-tracing.md): Wrap the source API to confirm the touch + capture the handler, Coverage to find the code path the handler runs, Logpoint at the check line to read the locals (e.g. `event.origin`).
- **"A trigger happened and I don't know which code ran"** (click, message, navigation) → **Coverage** (`coverage start` → trigger → `coverage stop`), then read the script URL + block ranges that ran.
- **"I know the source line and want to read the locals there"** → **Logpoint** (`logpoint add --url <u> --line <n> --col <n> --expr "<local>"`); the expression is evaluated in the paused frame's scope, so it reads locals by name without modifying behavior.

**Records wipe on navigation — dump before navigating again or the records are lost.** This applies to Wrap and Logpoint; Coverage is navigation-persistent (see [DYNAMIC-ANALYSIS.md](DYNAMIC-ANALYSIS.md)). **Logpoint's `--col` is required** — minified code packs many statements per line, and without the column the logpoint binds to the wrong statement.

For the full surface — command reference, record shapes, serialization rules, lifecycle, scope (incl. cross-origin iframe reach for Wrap vs Logpoint, Coverage's delta/cumulative semantics, navigation persistence), see [DYNAMIC-ANALYSIS.md](DYNAMIC-ANALYSIS.md).

For a worked example of Wrap + Coverage + Logpoint composing with page interaction to trace untrusted data from a DOM source to where it's checked or sinks, see [recipes/dom-data-flow-tracing.md](recipes/dom-data-flow-tracing.md).

## Traffic capture

`odda proxy-url` — Return the HTTP proxy URL as plain text. Route HTTP clients through this URL to capture traffic. Captured flows are stored as read-only files under `.odda/flows/`.

For the flow file layout, the `flows.jsonl` schema, and response-body decoding notes, see [FLOWS.md](FLOWS.md).

## Raw request crafting

`odda request` lets you craft and send raw HTTP requests byte-for-byte, bypassing the browser. Use it to replay/modify captured flows or send hand-built requests for header-injection, smuggling, and parser-differential tests.

Editable requests live in `.odda/requests/<name>/`:

- `request` — the raw HTTP request bytes (request line + headers + blank line + body), **CRLF-terminated**. For small edits use the built-in edit tool; for full-request rewrites or binary bodies use shell (`printf` or `cat`). **Ensure `\r\n` line endings** either way — heredocs use `\n` which will fail on the wire.
- `meta.json` — sidecar with `{"scheme": "http"|"https", "host": "...", "port": N}`. `send` uses this to open the socket; the `request` file is origin-form and carries no scheme/port. The `host` here is the TCP destination — it may intentionally differ from the `Host` header in the request file (for vhost/host-header/SSRF tests).

Commands:

- `odda request clone --flow-id <flow-id> --name <name> [--force]` — Copy `.odda/flows/<flow-id>/request` into `.odda/requests/<name>/request` and copy the flow's `meta.json` sidecar (scheme/host/port) into the editable request's `meta.json`. The `Host` header in the request file is left untouched and goes on the wire verbatim. Refuses to overwrite an existing request unless `--force`. Prints `name`/`path`/`flow_id`/`scheme`/`host`/`port` lines (text) or the same as JSON (`--json`).
- `odda request new --name <name> --host <host> [--protocol http|https] [--port <port>] [--line-terminator <bytes>] [--force]` — Create an empty `request` file (0 bytes) and a `meta.json` with the given host, protocol (default `https`), and port (default 80 for `http`, 443 for `https`). `--line-terminator` sets the byte sequence the parser splits header lines on (default `\r\n`; accepts `\n`, `\x00`, ...). H2-only: lets an agent put a literal CRLF inside an H2 header value (`:path`/`:authority`) for H2→H1 downgrade smuggling — the parser splits on the custom terminator, preserving the CRLF into the H2 frame. Ignored for HTTP/1.1 (wire-faithful). See [REQUEST.md](REQUEST.md) "Line terminator (H2 only)". Fill the `request` file with the edit tool. Prints `name`/`path`/`scheme`/`host`/`port` lines (text) or the same as JSON (`--json`).
- `odda request send --name <name> [--fix-content-length] [--timeout 30] [--insecure]` — Read both files, open a TCP socket (TLS for https, HTTP/2 when the request line says `HTTP/2`), write the exact bytes from the `request` file, read the response, decode it (de-chunk + gzip/br/deflate/zstd), and write a flow record to `.odda/flows/<NNNNN>/`. Output is the flow record (`id`/`method`/`scheme`/`host`/`port`/`path`/`status_code`/`total_duration_ms`/`body_file`/`error` as `key: value` lines in text, or the `flows.jsonl` record object with `--json`); read `.odda/flows/<id>/response_body.*` for the body. The response body is always stored, even for image/video/audio/font Content-Types that browser-capture drops (a hand-built request exists to see its body — e.g. a path-traversal file mislabeled `image/jpeg`). **Repeat `--name`** to send two or more requests on one connection (the multi-name pipeline): H1 request lines use sequential keep-alive (smuggling — response-queue poisoning, CL.0 confirmation); H2 request lines use concurrent stream-multiplex (multi-endpoint races). Multi-name `--json` returns a list of flow records; text renders one block per request. `--pipelining` (H1 multi-name only) sends all then reads all instead of send-then-read per request. **`--repeat N`** (single `--name` only) sends N concurrent copies of one request — the race / limit-overrun path (H2 single-packet stream-multiplex, or H1 parallel connections). See [REQUEST.md](REQUEST.md) for the multi-name behavior, `--pipelining`, `--repeat`, and the rejections. For a worked race-condition recipe, see [recipes/race-conditions.md](recipes/race-conditions.md).

For the `send` flags (`--fix-content-length`, `--timeout`, `--insecure`, `--pipelining`, `--repeat`), HTTP/2 framing, missing-body framing, and binary-body handling, see [REQUEST.md](REQUEST.md).

## Server commands

- `odda status` — Show server status, including socket path, data directory, proxy URL, and open browser count.
- `odda logs [--follow] [--n N]` — Show or tail the server log. Default `--n` is 50.
- `odda version` — Print the odda version.

`odda server`, `odda install`, and `odda init-chrome-profile` exist but are normally handled by the plugin, the package installer, and a one-time host setup step respectively, not by agents at runtime.