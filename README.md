# odda

`odda` is a toolkit for agent-driven web security research, composable from a single MCP server. It gives an AI agent a real Chrome it can drive and observe, a transparent mitmproxy that writes every flow to disk, and a raw-HTTP send path for smuggling and race conditions.

- **Drive Chrome** — open, navigate, run JS, screenshot, and interact with the page by accessibility-tree refs.
- **Capture every flow** — a transparent proxy writes every request/response to disk, including media bodies browser capture drops.
- **Observe JS in progress** — wrap functions/properties, plant source logpoints, record block coverage.
- **Craft raw HTTP** — byte-faithful sends for smuggling, parser-differentials, and races.

## Requirements

- Python 3.14+
- Chrome or Chromium installed and on `PATH`

## Installation

```bash
uv tool install odda
```

This installs the `odda` command (an MCP server plus one helper subcommand, with `--version` for the version probe). Then register the server with your harness — one MCP config entry, and every odda capability becomes typed tools your agent can call.

### Harness configuration

All configs spawn the same stdio server: `odda mcp`. One MCP process = one odda session with its own Chrome, proxy, and `.odda/` data dir (created lazily under the process's working directory on first use).

**Claude Code** — project `.mcp.json` (or `~/.claude.json` under `mcpServers`):

```json
{
  "mcpServers": {
    "odda": {
      "command": "odda",
      "args": ["mcp"]
    }
  }
}
```

**OpenCode** — `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "odda": {
      "type": "local",
      "command": ["odda", "mcp"]
    }
  }
}
```

**pi** — install the MCP adapter once (`pi install npm:pi-mcp-adapter`), then add to project `.mcp.json` (same `mcpServers` shape as Claude Code):

```json
{
  "mcpServers": {
    "odda": {
      "command": "odda",
      "args": ["mcp"]
    }
  }
}
```

### Chrome profile (one-time)

To give odda's isolated browser sessions a base profile (cookies, extensions, preferences):

```bash
odda init-chrome-profile
```

This opens a visible Chrome window pointed at odda's base profile directory. Log in, install extensions, and set preferences as you want them; close the window when done. odda copies this profile into each isolated browser session opened by `browser_open`. Skip this step to start from a clean profile each time.

## Usage

Agents drive odda through the MCP tools (`browser_open`, `navigate`, `page_snapshot`, `request_send`, ...). IDs are integers, monotonic within a session, and never reused; every browser/tab tool takes explicit `browser_id` / `tab_id` parameters.

A typical session:

```
browser_open          → {browser_id, tab_id}
navigate              → tab loads https://target.example/
read .odda/flows/flows.jsonl     → every request/response captured on disk
request_clone + request_send           → the Burp Repeater loop, as tools
```

All captured traffic is stored under `.odda/flows/<id>/` — request bytes, response body, headers, timing — plus the append-only `flows.jsonl` index. Grep the index to find flows; read `.odda/flows/<id>/response_body.*` for the body (including image/video/audio/font Content-Types that browser capture drops).

## Features

### Drive Chrome

A real Chrome (via patchright/Playwright), headless by default:

- **Open / navigate / eval / wait-for / screenshot** — drive the browser and run JS in the page.
- **Ref-driven page interaction** — `page_snapshot` returns the accessibility tree with `[ref=eN]` tags; pass the ref to `page_click` / `page_fill` / `page_hover` / `page_upload`. `page_snapshot` caps render depth — when the tree actually reaches the cap, cut points carry their hidden subtree depth (`[deeper=k]`) and the result ends with a note stating the real depth; `page_find` regex-searches the snapshot and returns matches with context instead of the whole tree (the cheap way to locate a ref on a large page); `boxes: true` adds element geometry for coordinate clicks. `page_click` and `page_hover` also accept viewport coordinates as a raw trusted event for targets the a11y tree can't name. Cross-iframe is transparent.
- **Per-browser userscripts** — JS that auto-runs at `document_start` on every navigation, before the page's own scripts.
- **Dialog blocking** — `alert` / `confirm` / `prompt` / `beforeunload` stay open until handled: the action that opens one returns its details, other tools on that tab wait, and every same-browser result lists open dialogs. Resolve with `dialog_handle` (accept/dismiss, prompt text); a human closing it in a headed window works too.

### Capture every flow

A transparent mitmproxy sits between Chrome and the network; driving the browser *is* traffic capture. Every request/response is saved under `.odda/flows/<id>/`; the index is `.odda/flows/flows.jsonl`. The proxy can also chain through an upstream forward proxy (`proxy_upstream_set`) to change the session's network vantage. See the `odda://docs/flows` resource for the file layout and schema.

### Observe JS in progress

Three lenses, chosen by what you know:

- **Wrap** — wrap a named function or property; record each call/access with `this`, `args`, `ret`, and `stack`. Takes effect on the next navigation.
- **Logpoint** — plant a non-pausing observation at a source `url` + `line` + `col`; the expression is evaluated in the paused frame's scope, so it reads locals by name.
- **Coverage** — record which code blocks execute across one or more navigations; start, trigger behavior, snapshot or stop.

Wrap and Logpoint records wipe on navigation — dump before navigating again. The `odda://docs/dynamic-analysis` resource documents the full surface; `odda://docs/recipes` has a worked example tracing untrusted DOM data to a sink.

### Craft raw HTTP

Byte-faithful raw HTTP sends, bypassing the browser — the Burp Repeater model, as tools:

- **Wire-verbatim HTTP/1.1** — the request file *is* the wire; nothing is re-framed.
- **HTTP/2 frame-source** — the request file is parsed into H2 frames; a custom line terminator lets a literal CRLF live inside an H2 pseudo-header for downgrade smuggling.
- **Clone or craft** — `request_clone` copies a captured flow's exact bytes; `request_new` starts an empty file.
- **Multi-name pipeline** — pass several names to send several requests on one connection (H1 keep-alive, or H2 concurrent stream-multiplex) for same-connection attacks.
- **Concurrent send** — `repeat` fires N copies of one request concurrently for race conditions and limit-overrun attacks.
- **`fix_content_length`** — recompute Content-Length after body edits.

See the `odda://docs/request-crafting` resource for framing details.

## How it works

- `odda mcp` is the **only** odda process: a stdio MCP server whose lifespan owns the proxy, the browser manager, and the flow storage. Your harness spawns it per agent session; closing the session tears everything down.
- The remaining CLI surface is helpers, not the automation surface: `odda init-chrome-profile` (interactive, human-run) and `odda --version`/`-V`.
- Project state lives in `.odda/` under the MCP process's working directory: `flows/`, `requests/`, `browsers/`. It's created lazily on the first state-producing call, not when the server boots.
- Errors from tools are odda's messages verbatim (as tool errors); unanticipated crashes log their traceback to the server's stderr, which the harness captures.