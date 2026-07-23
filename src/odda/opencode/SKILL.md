---
name: odda
description: Browser automation, HTTP traffic capture, dynamic analysis, and raw request crafting via the odda CLI. Use when driving Chrome, capturing flows, observing JS execution, or hand-building HTTP requests.
---

`odda` is a CLI tool for browser automation and HTTP traffic capture. The OpenCode plugin starts the odda server automatically when the session begins and exposes `ODDA_SOCKET` and `ODDA_DATA_DIR` environment variables for all shell tools. You almost never need to run `odda server` yourself.

All commands output JSON by default. Errors are returned as JSON `{error: ...}` with a non-zero exit code (unknown browser/tab, navigation failure, screenshot failure, "tab closed during operation" when another agent closes the tab mid-op). JS-execution errors (script throws) still return as strings inside a successful response; `wait-for` timeout raises a timeout error. Global options `--socket` and `--data-dir` are set by the plugin via environment variables, so they can normally be omitted.

## Targeting model

Every browser/tab command takes an explicit target. Agents always specify which browser and which tab they mean.

- **Browser-scoped** commands take `--browser-id`: `browser open`, `browser close`, `tabs list` (optional filter), `tabs open`, `userscript install`/`remove`.
- **Tab-scoped** commands take both `--browser-id` and `--tab-id`: `navigate` (existing tab), `eval`, `wait-for`, `screenshot`, `event-listeners`, `tabs close`, `coverage *`, `wrap *`, `logpoint *`, `page *`.

IDs are integers, monotonic, and **never reused**. A closed tab's id is retired forever; a stale `--tab-id` errors cleanly instead of silently hitting a different tab. This makes it safe for multiple agents to share one odda server: each agent owns the IDs it captured and never disturbs another agent's target.

Get IDs once and reuse them:
- `odda browser open` returns `{browser_id, tab_id, status}` — the initial tab is ready to use immediately.
- `odda tabs open --browser-id B [--url U]` returns `{browser_id, tab_id, status}`.
- `odda tabs list` returns `[{browser_id, tabs: [{tab_id, url, title}]}]`.

## Quick command reference

Scoped by section below: browser-scoped commands take `--browser-id`; tab-scoped commands take `--browser-id` + `--tab-id`. See the Targeting model above.

Add the targeting flags from the Targeting model above to every command below — `--browser-id <id>` for browser-scoped, `--browser-id <id> --tab-id <n>` for tab-scoped. Only the per-command flags are shown here.

| What you want to do         | Command                                                            |
| --------------------------- | ----------------------------------------------------------------- |
| Check the server is running | `odda status`                                                      |
| Get the HTTP proxy URL      | `odda proxy-url`                                                   |
| Open a Chrome window        | `odda browser open [--headless]`                                  |
| Close a browser             | `odda browser close`                                              |
| List tabs (overview)        | `odda tabs list`                                                  |
| Open a new tab              | `odda tabs open [--url <url>]`                                    |
| Close a tab                 | `odda tabs close`                                                 |
| Navigate an existing tab    | `odda navigate --url <url>`                                       |
| Run JavaScript              | `odda eval --js "<js>"` or `--file <path>`                        |
| Wait for a JS condition     | `odda wait-for --expression "<expr>" [--timeout N]`               |
| Screenshot                  | `odda screenshot [--output <path>]`                               |
| List event listeners        | `odda event-listeners`                                            |
| Snapshot (find refs)        | `odda page snapshot`                                              |
| Click by ref                | `odda page click --ref <ref> [--timeout N]`                       |
| Fill by ref                 | `odda page fill --ref <ref> --value "<value>" [--timeout N]`      |
| Hover by ref                | `odda page hover --ref <ref> [--timeout N]`                       |
| Upload files by ref         | `odda page upload --ref <ref> --file <path> [--file <path>...]`   |
| Install a userscript        | `odda userscript install --name <name> --file <path>`             |
| List userscripts            | `odda userscript list`                                            |
| Remove a userscript         | `odda userscript remove --name <name>`                            |
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
| Create an empty request     | `odda request new --name <name> [--force]`                         |
| Send an editable request    | `odda request send --name <name> [flags]`                          |

## Browser automation

Drives Chrome: open/navigate, run JS, take screenshots, and interact with the page by snapshot+ref. All tab-scoped unless noted.

- `odda browser open [--headless]` — Open a new Chrome window. Returns `{browser_id, tab_id, status}`; the initial tab is ready immediately. `--headless` runs Chrome without a visible window (CI/automated testing).
- `odda browser close` — Close a browser instance. Tearing down is immediate; any in-flight tab ops on that browser error cleanly. For an overview of all browsers and their tabs use `odda tabs list` (no `--browser-id`); `odda status` reports the open browser count.
- `odda tabs list [--browser-id <id>]` — List tabs grouped by browser as `[{browser_id, tabs: [{tab_id, url, title}]}]`. Without `--browser-id`, lists every open browser (a browser with zero tabs appears with `tabs: []`); with it, lists one browser's tabs.
- `odda tabs open [--url <url>]` — Open a new tab in the target browser. Without `--url` the tab opens at `about:blank`. Returns `{browser_id, tab_id, status}`.
- `odda tabs close` — Close an individual tab. Closing the **last** tab leaves the browser open with zero tabs (matching Chrome's behavior); the browser can still accept `tabs open` later. To close the whole browser use `odda browser close`.
- `odda navigate --url <url>` — Navigate an existing tab to a URL. To open a tab, use `odda tabs open`. Navigation failures (network error, invalid URL, or `load`-event timeout) raise a JSON error with non-zero exit; the timeout error message points at `odda wait-for` for SPAs whose `load` event never fires.
- `odda eval --js "<js>"` — Execute JavaScript in the target tab and return the result. Pass an expression, not a `return` statement (`return` is illegal at the top level — use an IIFE `(()=>{ ... })()` if you need statements). Returned Promises are awaited automatically: `fetch(url).then(r => r.status)` returns `200`, not a Promise object. Return a serializable value from async expressions — bare `fetch(url)` returns `{}` because the resolved `Response` is not JSON-serializable; chain `.then(r => r.text())` or similar. `--file <path>` loads JavaScript from a file (mutually exclusive with `--js`; useful for multi-line scripts and shell-escape avoidance). **`odda eval` JSON.stringify-encodes the result; calling `JSON.stringify` inside your JS double-encodes — return a plain object and let odda do the outer stringify.**
- `odda wait-for --expression "<expr>" [--timeout N]` — Poll a JS expression until it's truthy or the timeout (default 30s) is reached. Uses Playwright's `wait_for_function`, which polls in-browser with no round-trips. Runs in the main world, so it sees page globals and userscript-injected helpers. Returns the truthy value on success; errors with non-zero exit code on timeout. Example: `odda wait-for --expression "document.querySelector('.sdk-ready')" --timeout 10`.
- `odda screenshot [--output <path>]` — Capture a JPEG screenshot of the target tab's viewport. Returns the path to the screenshot file (a bare string). By default a temp file is generated; pass `--output <path>` to write to a path you choose (the parent directory is created).
- `odda event-listeners` — List JavaScript event listeners attached to `window` and `document` in the target tab.

### Page interaction (snapshot + ref)

`odda page` drives the browser to trigger behavior — clicking, filling, hovering, uploading, and snapshotting the page to find targets. The workflow is: **snapshot** to discover element **refs**, then pass the ref to the action command. This is the ref-driven alternative to `odda eval` with hand-written CSS selectors, which is brittle on minified SPAs. Page interaction is both standalone (browser automation) and composes with Dynamic analysis: snapshot to find the target, click/fill/upload to trigger behavior, then Wrap/Coverage/Logpoint to observe what happened.

A **ref** (`eN`, or `f<frameSeq>eN` inside an iframe) is a short-lived name for one element in a snapshot. The agent passes `eN` via `--ref` to `click`, `fill`, `hover`, and `upload`; odda resolves it to the element via Playwright's `aria-ref` selector engine.

- **Valid as long as the element stays in the DOM.** If the element is removed (SPA content swap, navigation), the action errors cleanly. Re-snapshot to discover refs for new elements; existing refs continue to work without re-snapshotting.
- **Cross-iframe is transparent.** Refs inside iframes have the form `f<frameSeq>eN`; odda resolves them automatically, no special handling.
- **File inputs without an accessible name do not appear in the snapshot.** Playwright omits nameless `<input type="file">` from the a11y tree. Give the input an `aria-label` via `eval`, re-snapshot, then upload by ref. File inputs render as `button` elements in the a11y tree after labeling — upload by that ref. Example: `odda eval --js "document.querySelector('input[type=file]').setAttribute('aria-label','upload')"` → re-snapshot → upload by ref.
- **Dialog interception.** odda auto-dismisses `window.alert`/`confirm`/`prompt`/`print` calls (a built-in userscript intercepts them). If `page click` triggers a `prompt()` or `confirm()`, the dialog is auto-dismissed (returns `null`/`false`) and the call is recorded in `window.__oddaDialogs` — check it via `eval --js "window.__oddaDialogs"` to see what happened. For forms that submit via a dialog prompt, use `eval` with `fetch` to submit programmatically instead. See [USERSCRIPTS.md](USERSCRIPTS.md) for the interceptor details.

Commands (all tab-scoped):

- `odda page snapshot` — Return the page's accessibility tree as YAML-ish text with `[ref=eN]` tags. The agent greps the text for the element it wants. No filter options; the full tree is returned. Prefer `page snapshot` over `odda screenshot` for finding elements and understanding page structure: the snapshot is text (cheaper on context, grep-able, carries refs for action commands), while the screenshot is pixels (useful only for visual layout, icons, or canvas the a11y tree can't see). Use screenshots when you need to see what the page looks like; use snapshots when you need to find an element to act on.
- `odda page click --ref <ref> [--timeout N]` — Click the element identified by `ref` (plain left-click). Returns `{status: "clicked", ref: "<ref>"}`. If the ref no longer resolves, errors cleanly with a stale-ref message.
- `odda page fill --ref <ref> --value "<value>" [--timeout N]` — Fill the element identified by `ref` with `value`. Clears the field first, then types. Works on text inputs, textareas, contenteditable elements, checkboxes (`"true"`/`"false"`), radios, and selects. Returns `{status: "filled", ref: "<ref>"}`.
- `odda page hover --ref <ref> [--timeout N]` — Hover the element identified by `ref`. Auto-scrolls the element into view first. Returns `{status: "hovered", ref: "<ref>"}`.
- `odda page upload --ref <ref> --file <path> [--file <path>...] [--timeout N]` — Set files on a file input identified by `ref`. Repeat `--file` for multiple files (`<input type="file" multiple>`). Returns `{status: "uploaded", ref: "<ref>", files: ["<path>", ...]}`. **This sets the file on the input but does not submit the form** — click the form's submit button by ref separately to POST it.

All action commands accept `--timeout` (default 5 seconds) for ref resolution and the action itself. This is shorter than `wait-for`'s 30s default because actions are interactive — the agent wants to know quickly when something didn't work. A stale ref errors within the timeout, not after a 30-second Playwright hang.

Navigations driven by `odda navigate` or `page click` flow through odda's proxy and are captured as flows — you can read the response body from `.odda/flows/<id>/response_body.*` (see [FLOWS.md](FLOWS.md)) instead of extracting it from the page via `eval`.

## Userscripts (cross-cutting)

`odda userscript` manages JavaScript helpers that auto-run at `document_start` on every navigation, before the page's own scripts. Install a helper once and it runs before the page's own scripts on every `odda navigate` and `odda tabs open`, in the main world. Useful for both browser automation (inject helpers) and dynamic analysis (Wraps are userscripts; see [USERSCRIPTS.md](USERSCRIPTS.md)).

- `odda userscript install --name <name> --file <path>` — Install a JS file as a userscript into the given browser's scope. Overwrites any existing userscript of the same name and reloads that browser's extension. Alternatively, use `--source "<js>"` for inline source (mutually exclusive with `--file`).
- `odda userscript list` — List installed userscripts for the given browser with their names and sizes.
- `odda userscript remove --name <name>` — Remove a userscript from the given browser's scope and reload its extension. The script's effects on the current page are not undone; it won't run on future navigations.

For per-browser storage internals, the built-in dialog interceptor (records `window.print`/`alert`/`confirm`/`prompt` calls into `window.__oddaDialogs`), and default userscripts, see [USERSCRIPTS.md](USERSCRIPTS.md).

## Dynamic analysis

Observing JavaScript execution in progress — recording what code does as it runs, with the intent to observe rather than modify. Three peer concepts, chosen by what you know:

- **Wrap** — "I know the function/property" → wrap it and record each call/access.
- **Logpoint** — "I know the line" → plant a non-pausing observation at a source location.
- **Coverage** — "I know neither" → record which code blocks execute, then find the path.

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

- `odda request clone --flow-id <flow-id> --name <name> [--force]` — Copy `.odda/flows/<flow-id>/request` into `.odda/requests/<name>/request` and copy the flow's `meta.json` sidecar (scheme/host/port) into the editable request's `meta.json`. The `Host` header in the request file is left untouched and goes on the wire verbatim. Refuses to overwrite an existing request unless `--force`.
- `odda request new --name <name> --host <host> [--protocol http|https] [--port <port>] [--force]` — Create an empty `request` file (0 bytes) and a `meta.json` with the given host, protocol (default `https`), and port (default 80 for `http`, 443 for `https`). Fill the `request` file with the edit tool.
- `odda request send --name <name> [--fix-content-length] [--timeout 30] [--insecure]` — Read both files, open a TCP socket (TLS for https, ALPN `h2` when the request line says `HTTP/2`), write the exact bytes from the `request` file, read the response, decode it (de-chunk + gzip/br/deflate/zstd), and write a flow record to `.odda/flows/<NNNNN>/`. Output is the `flows.jsonl` record that was appended; read `.odda/flows/<id>/response_body.*` for the body.

For the `send` flags (`--fix-content-length`, `--timeout`, `--insecure`), HTTP/2 framing, missing-body framing, and binary-body handling, see [REQUEST.md](REQUEST.md).

## Server commands

- `odda status` — Show server status, including socket path, data directory, proxy URL, and open browser count.
- `odda logs [--follow] [--n N]` — Show or tail the server log. Default `--n` is 50.
- `odda version` — Print the odda version.

`odda server` and `odda install-opencode` exist but are normally handled by the plugin and the package installer, not by agents at runtime.