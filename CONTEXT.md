# odda

## Language

## Harness integrations

**Harness**:
An external coding-agent tool that connects to odda as an MCP client (OpenCode, pi, omp, Claude Code). The harness spawns `odda mcp` per agent session; one MCP process is one odda session with its own Chrome, proxy, and data dir. Config is one MCP-server entry in the harness's own config file — odda ships nothing into harness config trees.
_Avoid_: host, agent runner, editor, IDE

**MCP session**:
One `odda mcp` process, spawned by a harness, from initialize to shutdown. The session owns the proxy, the browser manager, and the flow store; per-session isolation means nothing crosses session boundaries. Tearing the session down (client disconnect, harness exit) tears down its browsers and proxy.
_Avoid_: server instance, odda server, daemon

## Storage locations

**Data dir**:
`.odda/` under the MCP process's working directory — holds persisted project state: `flows/`, `requests/`, `browsers/`. Created lazily on the first state-producing call (browser open, request clone, etc.), not when the session starts. Absent until odda is actually used; read-only calls (`version`, `proxy_url`) never create it.
_Avoid_: project dir, state dir, .odda dir

## Browser sessions

**UA normalization**:
Launch-time behavior of headless browsers: odda derives a headed Chrome User-Agent from the installed Chrome's real version and the driver applies it across the whole browser context, so the wire User-Agent header and page-visible `navigator.userAgent` both present a headed browser. Covers every page of a headless browser, including tabs opened later; headed launches keep Chrome's own UA.
_Avoid_: UA spoofing, stealth mode, user-agent override

## Page interaction

**Page interaction**:
Driving the browser to trigger behavior — clicking, filling, hovering, uploading, and snapshotting the page to find targets. The agent uses Page interaction standalone for browser automation, or as the trigger step before Dynamic analysis (snapshot to find the target, click/fill/upload to trigger, then Wrap/Coverage/Logpoint to observe what happened).
_Avoid_: browser action, actuation, page automation, interaction layer

**Snapshot**:
A text serialization of the page's accessibility tree returned by the `page_snapshot` tool (Playwright's `page.aria_snapshot(mode="ai")`). Lists page elements with their roles, names, and their per-element **Ref** in `[ref=eN]` tags. Reaches into iframes transparently (iframe elements get refs of the form `f<frameSeq>e<elemNum>`). Always the whole page — never a subtree; capping render depth or asking for element boxes yields still just a Snapshot. A depth-capped Snapshot announces its cap: lines at the cut carry the hidden subtree depth and the result notes the tree's real depth, so a partial tree is never mistaken for the whole page. The agent reads the snapshot to find elements, uses **Find** to search it without pulling the whole tree, or **Diff** to re-read only what changed; the tree behind all three is never filtered.
_Avoid_: aria snapshot, accessibility tree, page snapshot, DOM dump

**Find**:
A search over a freshly taken Snapshot: the agent supplies a regex, odda returns the matching regions with surrounding context and their Refs — not the whole tree. The cheap way to locate a target, and its Ref, on a large page. Opt-in narrowing of what the agent reads; the Snapshot itself stays unfiltered.
_Avoid_: snapshot grep, search, filter

**Diff**:
A re-read of a Snapshot that returns only what changed since the tab's previous same-depth Snapshot: `-` lines from the previous render, `+` lines from the fresh one (their refs are the fresh, actionable ones), each hunk headed by its ancestor path. Unchanged content is never re-sent; nothing changed returns a one-line sentinel rather than an empty result. Every `page_snapshot` becomes the new baseline for its depth; navigation clears a tab's baselines, and **Find** never touches them. A Diff with no baseline (first call, or first after navigation) returns the whole tree, announced as such.
_Avoid_: delta, incremental snapshot, change report

**Ref**:
A short-lived name (`eN`, or `f<frameSeq>eN` inside an iframe) for one element in a snapshot. The agent passes `eN` as the `ref` parameter to `page_click`, `page_fill`, `page_hover`, and `page_upload` to identify the target; odda resolves it to the element via Playwright's `aria-ref` selector engine. A ref is valid as long as its element remains in the DOM; if the element is removed (SPA content swap, navigation), the action errors cleanly. Re-snapshot to discover refs for new elements; existing refs continue to work without re-snapshotting.
_Avoid_: element reference, aria-ref, handle, snapshot ref, locator

**Coordinate targeting**:
The alternative to Ref for `page_click` and `page_hover`: the agent passes viewport coordinates (`x`/`y`, CSS pixels from the viewport's top-left) instead of a ref, and odda dispatches a raw trusted mouse event at that point — no element resolution, no actionability checks, no timeout. The event lands on whatever renders there (iframes included); clicking empty space succeeds as a no-op. Coordinates come from a Snapshot's element boxes, a screenshot, or a `getBoundingClientRect` read. Use it when the a11y tree can't name the target: canvas, custom hit-testing, elements behind overlays, deliberate off-center clicks.
_Avoid_: pixel targeting, mouse targeting, coord click, position click

**Screenshot**:
A JPEG of a tab's viewport returned by the `screenshot` tool — pixels for reading visual layout, the counterpart of a **Snapshot** (which names actionable elements). An annotated Screenshot draws a numbered marker on every **Ref**'s bounding box, and the response carries a CSV legend mapping each marker to its ref and viewport box — the visual counterpart of a Snapshot's boxes, pairing pixels with refs and coordinates for **Coordinate targeting**; markers are per-capture labels sharing the image's lifetime, not Refs. Annotation is drawn onto the captured pixels, never by injecting anything into the page. The written path is always the result's first text block; the pixels are additionally returned inline by default, with `return_image=False` collapsing the result to the path (plus the legend when annotating).
_Avoid_: picture, screen capture, page image, overlay, ref overlay, labeled screenshot, snapshot (reserved for the accessibility-tree Snapshot)

## Dynamic analysis

**Dynamic analysis**:
Observing JavaScript execution in progress — recording what code does as it runs, with the intent to observe rather than modify.
_Avoid_: trace, analyze-js, instrumentation

**Userscript**:
A JS helper that auto-runs at `document_start` on every navigation, before the page's own scripts, in the main world. Installed via the `userscript_install` tool and re-injected on every page load. odda ships built-in default userscripts. Scope: per-browser, not shared across browsers; an agent opening a fresh browser starts with only the default userscripts.
_Avoid_: content script, extension script, injected helper, hook

**Logpoint**:
A placed observation at a source location the agent identifies by script URL, line, and column. odda plants a non-pausing `Debugger.setBreakpointByUrl` whose condition evaluates an expression the agent supplies, in the paused-then-immediately-resumed frame's scope. The page never stops. The expression can have side effects if the agent writes them, but the intent is to read, not write. odda warns at install time if no loaded script matches the URL. Logpoints persist until explicitly removed; records are wiped on navigation. Logpoints do not survive tab close — they are per-tab-session, not durable. Scope: same frame as the Debugger domain already enabled on (main frame and same-origin iframes). Cross-origin iframes and worker contexts are out of scope.
_Avoid_: breakpoint, tracepoint, watchpoint, probe

**Coverage**:
An aggregate query over a browsing context — start it, do the thing, stop it, read back per-block hit counts. Not placed at any target; records counts, not events. The recording window spans navigations: counts accumulate across page loads inside the `[start, stop]` window, so an agent can start, navigate to trigger behavior, and snapshot/stop to read which paths ran. Scope: main frame and same-origin iframes in a tab. Cross-origin iframes and worker contexts are out of scope.
_Avoid_: probe, profile, execution map, wrap

## Request crafting

**Multi-name pipeline**:
The sequential mode of `request_send` when two or more names are given: one HTTP/1.1 connection, requests sent and responses read in order, connection kept open across sends. Serves same-connection attacks (response-queue poisoning, CL.0 confirmation, H1 request smuggling). `pipelining` switches to send-all-then-read-all (true H1 pipelining) for victim-consumption smuggling. Single-name `request_send` (one name, no `repeat`) is a separate, frozen single-shot contract, not a pipeline.
_Avoid_: pipeline, request pipeline, send pipeline

**Concurrent send**:
The concurrent mode of `request_send`, used for race-condition / limit-overrun attacks where N requests must arrive at the server near-simultaneously to slip through a check-then-write window. Two input shapes: `repeat: N` (one request, N copies — the limit-overrun and rate-limit-bypass pattern) and multi-name over HTTP/2 (N different requests as concurrent streams — the multi-endpoint race pattern). HTTP/2 uses stream multiplexing with the last-byte single-packet technique (all N HEADERS frames in one TLS record) so the requests arrive atomically; HTTP/1.1 cannot multiplex on one connection, so `repeat` opens N parallel connections. Distinct from the Multi-name pipeline, which is sequential and HTTP/1.1-only.
_Avoid_: race send, parallel send, single-packet send, stream multiplex

**Wire-faithful**:
An HTTP/1.1 request file whose bytes go on the socket verbatim. The parser extracts metadata (method, path, Content-Length, Host) only for bookkeeping; the file is the wire bytes. A byte sequence in an H1 `Host` value already goes on the wire as-is — no framing knob is needed or meaningful for H1.
_Avoid_: verbatim, raw-send, byte-accurate

**Frame-source**:
An HTTP/2 request file that is parsed into header values and translated to H2 frames; the file's bytes never touch the socket. The file is a source for frame construction, not a wire transcript. This is why the line terminator is load-bearing for H2 (it determines how odda splits the file into header values, which determines what bytes land in each frame) and meaningless for H1 (wire-faithful — the file is the wire bytes). The H1-shaped text with `Host` is odda's input syntax for the `:authority` pseudo-header (RFC 9113 §8.3.1 forbids `Host` in H2; odda translates `Host` → `:authority` at send time), not a wire transcript.
_Avoid_: H2 source, frame input, pseudo-header source

**Line terminator**:
The byte sequence ending a header line in the request file, stored in `meta.json` (`line_terminator`, default `\r\n`) and set at `request_new` time. The block terminator (header/body separator) is always two line terminators. Only honored for frame-source (HTTP/2) request files; ignored on HTTP/1.1 (wire-faithful — the file is the wire bytes, no re-framing). Lets an agent put a literal CRLF inside a pseudo-header value (e.g. a `:path` of `/foo\r\nX-Evil: yes` for H2→H1 downgrade smuggling) by setting the line terminator to `\n`: the parser splits on `\n` / `\n\n`, never on `\r\n`, so the CRLF inside the value is preserved into the H2 frame. The agent's constraint is that no value contains the terminator.
_Avoid_: separator, delimiter, line separator, framing byte

## Dialogs

**Dialog**:
A JavaScript modal (`alert`, `confirm`, `prompt`, `beforeunload`) that the page opens. Under odda, a dialog is a first-class open state of its tab: it stays open (never auto-accepted, never auto-dismissed), it surfaces in every same-browser tool result while open, and it gates the tab — tab-touching tools block until it is handled. The action that opened it returns with the dialog's details instead of hanging.
_Avoid_: dialog interceptor, notification, popup

**Dialog note**:
The structured record of open dialogs a tool result carries while any dialog is open in the same browser: `dialogs: [{tab_id, type, message, default_value}]` appended to same-browser tool results, and `closed_dialog: {type, message}` on a `tabs_close` that closed a dialog tab. The note is read-only observation — it never handles the dialog.
_Avoid_: dialog response, modal state, dialog hint

**Dialog handling**:
Resolving an open dialog via the `dialog_handle` tool (`accept`/`dismiss`, `prompt_text` for prompts). The only way an agent changes a dialog's outcome; handling resumes the tab's frozen page JS, completes parked actions, and unblocks same-tab tools. A human closing the dialog in a headed window resolves it just as well: the tab unblocks (odda detects the close via a parked liveness probe and clears its dialog state), and a subsequent `dialog_handle` reports the dialog as already closed rather than double-handling it.
_Avoid_: dialog response, auto-accept, dialog handler

## Proxy interception

**Proxy-script**:
A Python file in mitmproxy `-s` script format that odda execs and adds to the running proxy's addon chain. Installed via the `proxy_script_install` tool (`force` overwrites by name), persisted under `.odda/proxy-scripts/<name>/script.py`, re-added on session boot; scope is global (one proxy shared across all browsers, not per-browser like userscripts).
_Avoid_: addon, interceptor, proxy addon, mitmproxy script, userscript

## Testing

**Test module**:
A pytest module under `tests/e2e/` (`test_NN_<slug>.py`, one per former scrut document) that exercises a slice of odda's tool surface. Each test enters its own MCP session with its own data dir, so no state crosses test boundaries.
_Avoid_: test file, test script, test document

**Test container**:
A Docker image, built from `python:3.14-slim` + Google Chrome, kept as the seed for a future CI runner — not part of the documented workflow. The suite itself runs on the host against the host's Chrome (already required for usage).
_Avoid_: test image, test box

**Per-test session**:
An MCP session entered inside a single test's body (in-process `Client` over the server object), owning one Chrome, one proxy, and one tmp data dir. Torn down at the test's end. No session state crosses test boundaries.
_Avoid_: shared session, module fixture, per-doc server

**Fixture server**:
A local server bound to `127.0.0.1` on an auto-picked port, started by a test as a real subprocess to provide a URL for browser navigation or proxy capture. Variants: a static file server, the dyn server, and raw-socket script servers. Torn down at the test's end.
_Avoid_: mock server, test server, httpd

**Dyn server**:
A local HTTPS server bound to `127.0.0.1` on an auto-picked port, started as a real subprocess to provide an HTTP/1.1 and HTTP/2 target with TLS and dynamic responses (`?body=&status=&header=&gzip=1`). Run under hypercorn (a dev dependency) serving a small ASGI app with a self-signed cert generated at launch. Serves the same dynamic-response contract as the remote `xs2.top` testing server, but with no external network dependency.
_Avoid_: mock server, H2 server, xs2 server, dynamic fixture