# odda

## Language

## Harness integrations

**Harness**:
An external coding-agent tool that odda integrates with by shipping a plugin (auto-starts the odda server at session load, injects `ODDA_*` env vars into shell tool calls) plus a skill (teaches the agent the odda CLI surface). odda integrates with OpenCode, pi, omp, and Claude Code.
_Avoid_: host, agent runner, editor, IDE

**Plugin**:
The lifecycle piece odda ships to a harness: it spawns the per-PID odda server at session start and makes `ODDA_SOCKET`/`ODDA_DATA_DIR`/`ODDA_LOG` available to every shell tool call. The form is per-harness, not a fixed artifact: a TypeScript module for OpenCode (`plugin.js`), pi (`plugin-pi.ts`, `process.env` inheritance), omp (`plugin-omp.ts`, per-call `tool_call` env injection), and a self-contained first-class plugin bundle for Claude Code (a `.claude-plugin/plugin.json` manifest plus a `SessionStart` hook in `hooks/hooks.json` and the shared skill, written to `~/.claude/skills/odda/` which Claude Code auto-loads as a skills-directory plugin — no marketplace, and the user's `~/.claude/settings.json` is never touched; the hook writes `export ODDA_SOCKET=…` etc. into `$CLAUDE_ENV_FILE`, which Claude Code…
_Avoid_: extension, add-on, mod. (Claude Code calls its own lifecycle callbacks "hooks"; odda's Plugin *uses* a Claude Code hook but is not itself named "hook" in odda's language.)

**Skill**:
A directory with a `SKILL.md` (Agent Skills standard) describing the odda CLI surface. One shared skill source lives at `src/odda/harness/skill/`; the install command copies it verbatim into each harness's first-party skill dir (`~/.pi/agent/skills/odda/`, `~/.omp/agent/skills/odda/`, `~/.config/opencode/skills/odda/`, or `~/.claude/skills/odda/skills/odda/` inside the Claude Code plugin bundle). Per-harness install targets keep each integration self-contained in its own config tree; the skill *source* is shared in the repo, the install *target* is not. The skill is the contract the agent reads to learn odda's commands; the plugin only handles lifecycle, never teaches commands.
_Avoid_: docs, instructions, prompt

## Storage locations

**Session dir**:
`$XDG_RUNTIME_DIR` — holds the per-PID Unix socket (`odda-<pid>.sock`) and the per-PID server log (`odda-<pid>.log`). Ephemeral: tied to one odda server process, cleaned on logout/reboot. Created by the harness plugin (or whoever starts the server); the data dir is not. `odda logs` finds the log here via `ODDA_LOG` or `status.log_path`.
_Avoid_: runtime dir, socket dir, temp dir

**Data dir**:
`.odda/` in the project directory (or `--data-dir` / `ODDA_DATA_DIR`) — holds persisted project state: `flows/`, `requests/`, `browsers/`. Created lazily on the first state-producing write (browser open, request clone, etc.), not when the server boots. Absent until odda is actually used; read-only commands (`status`, `version`, `logs`, `proxy-url`) never create it.
_Avoid_: project dir, state dir, .odda dir

## CLI output

**Text output**:
The default output of every odda CLI command — human-readable text rendered by a hand-written per-command formatter (tables for lists, `key: value` for flat dicts, raw lines for `logs`, YAML-ish blocks for `wrap dump` / `logpoint dump` / `coverage snapshot` / `coverage stop`). Free to drift for readability; not a parse target. Errors print as `Error: <message>` on stderr with a non-zero exit code in text mode.
_Avoid_: human output, pretty output, default output, rendered output

**Structured output**:
The JSON output produced when a command is invoked with the global `--json` flag. The parse target for scripts and agents doing structural queries (notably `wrap dump` / `logpoint dump` / `coverage snapshot` / `coverage stop`, whose nested records a flat text format cannot express). Stable by convention — field names and shapes are not broken casually — but with no versioned schema contract. Errors print as `{"error": ...}` on stdout with a non-zero exit code in structured mode.
_Avoid_: JSON output, machine output, raw output, --json output

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

**Coordinate targeting**:
The alternative to Ref for `click` and `hover`: the agent passes viewport coordinates (`-x`/`-y`, CSS pixels from the viewport's top-left) instead of a ref, and odda dispatches a raw trusted mouse event at that point — no element resolution, no actionability checks, no timeout. The event lands on whatever renders there (iframes included); clicking empty space succeeds as a no-op. Coordinates come from a screenshot or a `getBoundingClientRect` read. Use it when the a11y tree can't name the target: canvas, custom hit-testing, elements behind overlays, deliberate off-center clicks.
_Avoid_: pixel targeting, mouse targeting, coord click, position click

## Dynamic analysis

**Dynamic analysis**:
Observing JavaScript execution in progress — recording what code does as it runs, with the intent to observe rather than modify.
_Avoid_: trace, analyze-js, instrumentation

**Wrap**:
A placed observation at a function or property the agent names. odda replaces the function or property descriptor via a userscript injected at document_start, and records each call or access with its receiver (`this`), arguments, return value, and call stack. Functions in args/ret/this are serialized as `{type: "function", name, source}` where `source` is the function's `.toString()`, capped at 1000 chars. Records are wiped on navigation. Scope: all frames in a tab, including cross-origin iframes; per-browser, not shared across browsers. Does not reach worker contexts (web workers, service workers).
_Avoid_: hook, trap, intercept, monkey-patch, probe, breakpoint

**Userscript**:
A JS helper that auto-runs at `document_start` on every navigation, before the page's own scripts, in the main world. Installed via `odda userscript install` and re-injected on every page load. odda ships built-in default userscripts (notably the dialog interceptor recording `window.print`/`alert`/`confirm`/`prompt` calls into `window.__oddaDialogs`). Scope: per-browser, not shared across browsers; an agent opening a fresh browser starts with only the default userscripts.
_Avoid_: content script, extension script, injected helper, hook

## Request crafting

**Multi-name pipeline**:
The sequential mode of `odda request send` when two or more `--name` flags are given: one HTTP/1.1 connection, requests sent and responses read in order, connection kept open across sends. Serves same-connection attacks (response-queue poisoning, CL.0 confirmation, H1 request smuggling). `--pipelining` switches to send-all-then-read-all (true H1 pipelining) for victim-consumption smuggling. Single-name `send` (one `--name`, no `--repeat`) is a separate, frozen single-shot contract, not a pipeline.
_Avoid_: pipeline, request pipeline, send pipeline

**Concurrent send**:
The concurrent mode of `odda request send`, used for race-condition / limit-overrun attacks where N requests must arrive at the server near-simultaneously to slip through a check-then-write window. Two input shapes: `--repeat N` (one request, N copies — the limit-overrun and rate-limit-bypass pattern) and multi-name over HTTP/2 (N different requests as concurrent streams — the multi-endpoint race pattern). HTTP/2 uses stream multiplexing with the last-byte single-packet technique (all N HEADERS frames in one TLS record) so the requests arrive atomically; HTTP/1.1 cannot multiplex on one connection, so `--repeat` opens N parallel connections. Distinct from the Multi-name pipeline, which is sequential and HTTP/1.1-only.
_Avoid_: race send, parallel send, single-packet send, stream multiplex

**Wire-faithful**:
An HTTP/1.1 request file whose bytes go on the socket verbatim. The parser extracts metadata (method, path, Content-Length, Host) only for bookkeeping; the file is the wire bytes. A byte sequence in an H1 `Host` value already goes on the wire as-is — no framing knob is needed or meaningful for H1.
_Avoid_: verbatim, raw-send, byte-accurate

**Frame-source**:
An HTTP/2 request file that is parsed into header values and translated to H2 frames; the file's bytes never touch the socket. The file is a source for frame construction, not a wire transcript. This is why the line terminator is load-bearing for H2 (it determines how odda splits the file into header values, which determines what bytes land in each frame) and meaningless for H1 (wire-faithful — the file is the wire bytes). The H1-shaped text with `Host` is odda's input syntax for the `:authority` pseudo-header (RFC 9113 §8.3.1 forbids `Host` in H2; odda translates `Host` → `:authority` at send time), not a wire transcript.
_Avoid_: H2 source, frame input, pseudo-header source

**Line terminator**:
The byte sequence ending a header line in the request file, stored in `meta.json` (`line_terminator`, default `\r\n`) and set via `request new --line-terminator <bytes>`. The block terminator (header/body separator) is always two line terminators. Only honored for frame-source (HTTP/2) request files; ignored on HTTP/1.1 (wire-faithful — the file is the wire bytes, no re-framing). Lets an agent put a literal CRLF inside a pseudo-header value (e.g. a `:path` of `/foo\r\nX-Evil: yes` for H2→H1 downgrade smuggling) by setting the line terminator to `\n`: the parser splits on `\n` / `\n\n`, never on `\r\n`, so the CRLF inside the value is preserved into the H2 frame. The agent's constraint is that no value contains the terminator.
_Avoid_: separator, delimiter, line separator, framing byte

## Proxy interception

**Proxy-script**:
A Python file in mitmproxy `-s` script format that odda execs and adds to the running proxy's addon chain. Installed via `odda proxy-script install` (with `--force` to overwrite by name), persisted under `.odda/proxy-scripts/<name>/script.py`, re-added on server boot; scope is global (one proxy shared across all browsers, not per-browser like userscripts).
_Avoid_: addon, interceptor, proxy addon, mitmproxy script, userscript

**Dialog response**:
A pre-registered value the dialog interceptor returns for a `confirm` or `prompt` call instead of the default. Defaults: `confirm` returns `true`, `prompt` returns `"odda"` (proceed-by-default). The agent overrides per-type by setting `window.__oddaDialogResponses` (e.g. `{prompt: "s3cr3t"}`) via `eval` before the triggering click, or via a userscript for on-load prompts; the interceptor reads the map and returns the registered value, recording the dialog in `window.__oddaDialogs` with that `result`. To restore the old deny-by-default, register `{confirm: false, prompt: null}`. The interceptor never resets the map (agent-owned state); no map set = proceed-by-default. Per-tab: the map lives on `window`, scoped to one document. Keys for `alert`/`print` are ignored — those types have no return value to influence. Registered values pass through verbatim (no type coercion).
_Avoid_: dialog handler, dialog stub, dialog mock, prompt override

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