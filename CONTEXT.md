# odda

## Language

## Page interaction

**Page interaction**:
Driving the browser to trigger behavior — clicking, filling, hovering, uploading, and snapshotting the page to find targets. The agent uses Page interaction standalone for browser automation, or as the trigger step before Dynamic analysis (snapshot to find the target, click/fill/upload to trigger, then Wrap/Coverage/Logpoint to observe what happened).
_Avoid_: browser action, actuation, page automation, interaction layer

**Snapshot**:
A text serialization of the page's accessibility tree returned by `odda page snapshot` (Playwright's `page.aria_snapshot(mode="ai")`). Lists page elements with their roles, names, and a per-element **Ref** in `[ref=eN]` tags. Reaches into iframes transparently (iframe elements get refs of the form `f<frameSeq>e<elemNum>`). The agent greps the snapshot for elements it wants; odda does not filter.
_Avoid_: aria snapshot, accessibility tree, page snapshot, DOM dump

**Ref**:
A short-lived name (`eN`, or `f<frameSeq>eN` inside an iframe) for one element in a snapshot. The agent passes `eN` as a positional argument to `click`, `fill`, `hover`, and `upload` to identify the target; odda resolves it to the element via Playwright's `aria-ref` selector engine. A ref is valid as long as its element remains in the DOM; if the element is removed (SPA content swap, navigation), the action errors cleanly. Re-snapshot to discover refs for new elements; existing refs continue to work without re-snapshotting.
_Avoid_: element reference, aria-ref, handle, snapshot ref, locator

## Dynamic analysis

**Dynamic analysis**:
Observing JavaScript execution in progress — recording what code does as it runs, with the intent to observe rather than modify.
_Avoid_: trace, analyze-js, instrumentation

**Wrap**:
A placed observation at a function or property the agent names. odda replaces the function or property descriptor via a userscript injected at document_start, and records each call or access with its receiver (`this`), arguments, return value, and call stack. Records are wiped on navigation. Scope: all frames in a tab, including cross-origin iframes. Does not reach worker contexts (web workers, service workers).
_Avoid_: hook, trap, intercept, monkey-patch, probe, breakpoint

**Logpoint**:
A placed observation at a source location the agent identifies by script URL, line, and column. odda plants a non-pausing `Debugger.setBreakpointByUrl` whose condition evaluates an expression the agent supplies, in the paused-then-immediately-resumed frame's scope. The page never stops. The expression can have side effects if the agent writes them, but the intent is to read, not write. odda warns at install time if no loaded script matches the URL. Logpoints persist until explicitly removed; records are wiped on navigation. Logpoints do not survive tab close — they are per-tab-session, not durable. Scope: same frame as the Debugger domain already enabled on (main frame and same-origin iframes). Cross-origin iframes and worker contexts are out of scope.
_Avoid_: breakpoint, tracepoint, watchpoint, probe

**Coverage**:
An aggregate query over a browsing context — start it, do the thing, stop it, read back per-block hit counts. Not placed at any target; records counts, not events. The recording window spans navigations: counts accumulate across page loads inside the `[start, stop]` window, so an agent can start, navigate to trigger behavior, and snapshot/stop to read which paths ran. Scope: main frame and same-origin iframes in a tab. Cross-origin iframes and worker contexts are out of scope.
_Avoid_: probe, profile, execution map, wrap

**Test document**:
A Scrut Markdown file under `tests/e2e/scrut/` that exercises a slice of odda's CLI surface. Each document is independently runnable and gets its own working directory and odda server.
_Avoid_: test file, test script

**Test suite**:
The full collection of test documents in `tests/e2e/scrut/`, run together via `scripts/test-e2e.sh` inside the test container.
_Avoid_: test set, test collection

**Test container**:
A Docker image, built from `python:3.14-slim` + Google Chrome, that provides the complete environment for running the test suite. Contains `odda` installed on `$PATH`, `scrut`, Chrome, and all system tools the test docs need. Runs as a non-root user.
_Avoid_: test image, test box

**Per-doc server**:
An odda server instance booted by a single test document in its own `$PWD`, using a per-document socket path and data directory. Torn down at the document's end. No server state crosses document boundaries.
_Avoid_: shared server, global server

**Shared boot/teardown**:
The `_lib/boot.md` (prepended) and `_lib/teardown.md` (appended) files that DRY the odda server start and stop across all test documents. Each document still gets its own per-doc server instance; only the prose is shared.
_Avoid_: setup file, fixture file

**Fixture server**:
A `python3 -m http.server` bound to `127.0.0.1` on an auto-picked port, started by a test document to provide a URL for browser navigation or proxy capture. Serves files copied from `tests/e2e/scrut/fixtures/` into `$PWD/site/`. Torn down at the document's end via `pkill`.
_Avoid_: mock server, test server, httpd

**Dyn server**:
A local HTTPS server bound to `127.0.0.1` on an auto-picked port, started by a test document to provide an HTTP/1.1 and HTTP/2 target with TLS and dynamic responses (`?body=&status=&header=&gzip=1`). Run under hypercorn (a dev dependency) serving a small ASGI app (`tests/e2e/scrut/fixtures/dyn_asgi.py`) with a self-signed cert generated at launch. Serves the same dynamic-response contract as the remote `xs2.top` testing server, but with no external network dependency. Used by the `request clone` and `request send` test documents. Torn down at the document's end via `pkill`.
_Avoid_: mock server, H2 server, xs2 server, dynamic fixture

**Browser fixture**:
A headless odda browser instance plus an initial navigation to a fixture-server URL, set up at the start of a test document so subsequent `eval` / `screenshot` / `event-listeners` / `wrap` / `logpoint` / `coverage` / `page snapshot` / `page click` / `page fill` / `page hover` / `page upload` commands have a live target.
_Avoid_: browser setup, browser init, session

**Shared setup block**:
A scrut `_lib/*.md` file prepended (or appended) to every test document that needs it, providing a fixture server, browser fixture, dyn server, or teardown with prose DRY'd across docs. Each document still gets its own per-doc instance — only the prose is shared, mirroring Shared boot/teardown.
_Avoid_: shared fixture, shared steps