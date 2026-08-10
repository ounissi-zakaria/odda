# odda

`odda` is a toolkit for agent-driven web security research — Burp Suite and DevTools, composable from bash. It gives an AI agent a real Chrome it can drive and observe, a transparent mitmproxy that writes every flow to disk, and a raw-HTTP send path for smuggling and race conditions. Every command prints text both you and the agent can read.

- **Drive Chrome** — open, navigate, run JS, screenshot, and interact with the page by accessibility-tree refs.
- **Capture every flow** — a transparent proxy writes every request/response to disk, including media bodies browser capture drops.
- **Observe JS in progress** — wrap functions/properties, plant source logpoints, record block coverage.
- **Craft raw HTTP** — byte-faithful sends for smuggling, parser-differentials, and races.

## Requirements

- Python 3.14+
- Chrome or Chromium installed and on `PATH`

## Installation

```bash
uv tool install git+https://github.com/ounissi-zakaria/odda.git
odda install opencode   # or: odda install pi | odda install omp
```

The first command installs the `odda` CLI globally. The second copies the plugin + skill into your harness's config directory (`~/.config/opencode/`, `~/.pi/agent/`, or `~/.omp/agent/`) so an AI agent session auto-starts the odda server and learns the command surface.

### Chrome profile (one-time)

To give odda's isolated browser sessions a base profile (cookies, extensions, preferences):

```bash
odda init-chrome-profile
```

This opens a visible Chrome window pointed at odda's base profile directory. Log in, install extensions, and set preferences as you want them; close the window when done. odda copies this profile into each isolated browser session opened by `odda browser open`. Skip this step to start from a clean profile each time.

## Usage

From inside an OpenCode (or pi / omp) session, the plugin starts the server and injects `ODDA_SOCKET` / `ODDA_DATA_DIR` for you. A typical loop:

```bash
odda browser open                              # returns {browser_id, tab_id}
odda navigate --url https://example.com --browser-id 1 --tab-id 1
odda screenshot --browser-id 1 --tab-id 1      # writes a JPEG
odda eval --js "document.title" --browser-id 1 --tab-id 1
```

Every browser/tab command takes an explicit `--browser-id` and tab-scoped commands also take `--tab-id`. IDs are integers, monotonic, and never reused, so multiple agents can share one odda server without racing on a shared cursor.

### Replay and modify a captured request

All browser traffic is routed through odda's proxy and captured as flows under `.odda/flows/<id>/`. Clone one, edit the raw bytes, and resend it — the core "Burp Repeater" loop, from bash:

```bash
odda browser open
odda navigate --url https://target.example/ --browser-id 1 --tab-id 1
# Find the captured flow's id in the index:
rg target.example .odda/flows/flows.jsonl | tail -1
odda request clone --flow-id <flow-id> --name admin
# Have the agent edit .odda/requests/admin/request with its edit tool —
# set Host: localhost (keep CRLF line endings; heredocs emit \n and fail on the wire):
#   GET /admin HTTP/1.1\r\n
#   Host: localhost\r\n
#   Cookie: session=...\r\n
#   \r\n
odda request send --name admin                  # records the response as a new flow
# Read the response body:
cat .odda/flows/<new-id>/response_body.*
```

To run without a harness, start the server manually: `odda server --socket /tmp/odda.sock --data-dir ./.odda --parent-pid $$`, then export `ODDA_SOCKET=/tmp/odda.sock` before the client commands.

## Features

### Drive Chrome

A real Chrome (via patchright/Playwright), headless by default:

- **Open / navigate / eval / screenshot / wait-for** — drive the browser and run JS in the page.
- **Ref-driven page interaction** — `page snapshot` returns the accessibility tree with `[ref=eN]` tags; pass the ref to `page click` / `fill` / `hover` / `upload`. Cross-iframe is transparent.
- **Per-browser userscripts** — JS that auto-runs at `document_start` on every navigation, before the page's own scripts.
- **Dialog interceptor** — `alert` / `confirm` / `prompt` / `print` proceed by default and are recorded for inspection.

### Capture every flow

A transparent mitmproxy sits between Chrome and the network; driving the browser *is* traffic capture:

- Every request/response is saved under `.odda/flows/<id>/` — request bytes, response body, headers, timing.
- `flows.jsonl` is the append-only index; grep it to find flows by host, method, or path.
- Response bodies are stored as files — including image, video, audio, and font Content-Types that browser capture drops.
- Read the body directly: `.odda/flows/<id>/response_body.*`.

See the in-repo [FLOWS.md](src/odda/harness/skill/FLOWS.md) for the file layout and schema.

### Observe JS in progress

Three lenses, chosen by what you know:

- **Wrap** — wrap a named function or property; record each call/access with `this`, `args`, `ret`, and `stack`. Takes effect on the next navigation.
- **Logpoint** — plant a non-pausing observation at a source `url` + `line` + `col`; the expression is evaluated in the paused frame's scope, so it reads locals by name.
- **Coverage** — record which code blocks execute across one or more navigations; start, trigger behavior, snapshot or stop.

Wrap and Logpoint records wipe on navigation — dump before navigating again. See [DYNAMIC-ANALYSIS.md](src/odda/harness/skill/DYNAMIC-ANALYSIS.md) for the full surface, and [recipes/dom-data-flow-tracing.md](src/odda/harness/skill/recipes/dom-data-flow-tracing.md) for a worked example tracing untrusted DOM data to a sink.

### Craft raw HTTP

Byte-faithful raw HTTP sends, bypassing the browser — the Burp Repeater model, scriptable:

- **Wire-verbatim HTTP/1.1** — the request file *is* the wire; nothing is re-framed.
- **HTTP/2 frame-source** — the request file is parsed into H2 frames; a custom `--line-terminator` lets a literal CRLF live inside an H2 pseudo-header for downgrade smuggling.
- **Clone or craft** — `request clone --flow-id` copies a captured flow's exact bytes; `request new` starts an empty file.
- **Multi-name pipeline** — repeat `--name` to send several requests on one connection (H1 keep-alive, or H2 concurrent stream-multiplex) for same-connection attacks.
- **Concurrent send** — `--repeat N` fires N copies of one request concurrently for race conditions and limit-overrun attacks.
- **`--fix-content-length`** — recompute Content-Length after body edits.

See [REQUEST.md](src/odda/harness/skill/REQUEST.md) for framing details and [recipes/race-conditions.md](src/odda/harness/skill/recipes/race-conditions.md) for a worked race.

## How it works

- odda runs a **background server** (`odda server`) that holds the proxy, the browser instances, and the captured-flow state in one process.
- The **CLI** commands talk to it over a Unix socket via JSON-RPC — every `odda <command>` is one short-lived client call.
- A **harness plugin** (OpenCode / pi / omp) starts the server at session load and injects `ODDA_SOCKET` + `ODDA_DATA_DIR` into every shell the agent runs, so you almost never run `odda server` yourself.

Project state lives in `.odda/` in the working directory: `flows/`, `requests/`, `browsers/`. It's created lazily on the first state-producing command, not when the server boots.