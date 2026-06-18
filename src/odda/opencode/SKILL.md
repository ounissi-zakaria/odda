---
name: odda
description: Browser automation and HTTP traffic capture via the odda CLI
---

`odda` is a CLI tool for browser automation and HTTP traffic capture. The OpenCode plugin starts the odda server automatically when the session begins and exposes `ODDA_SOCKET` and `ODDA_DATA_DIR` environment variables for all shell tools.

You almost never need to run `odda server` yourself. Prefer the commands below.

All commands output JSON by default. Errors are returned as JSON with a non-zero exit code. Global options `--socket` and `--data-dir` are set by the plugin via environment variables, so they can normally be omitted.

## Quick command reference

| What you want to do         | Command                                                  |
| --------------------------- | -------------------------------------------------------- |
| Check the server is running | `odda status`                                            |
| Get the HTTP proxy URL      | `odda proxy-url`                                         |
| Open a Chrome window        | `odda browser open`                                      |
| See open browsers           | `odda browser list`                                      |
| Close a browser             | `odda browser close <id>`                                |
| List tabs                   | `odda tabs list` or `odda tabs list --browser-id <id>`   |
| Switch tab                  | `odda switch-tab --browser-id <id> --index <n>`          |
| Navigate                    | `odda navigate <url>` or `odda navigate <url> --new-tab` |
| Run JavaScript              | `odda eval "<js>"`                                       |
| Screenshot                  | `odda screenshot`                                        |
| List event listeners        | `odda event-listeners`                                   |
| Read server logs            | `odda logs [--follow] [--n N]`                           |

## Browser commands

- `odda browser open` — Open a new Chrome window. Returns the browser ID.
- `odda browser list` — List open browser instances with their IDs and active state.
- `odda browser close <id>` — Close a browser instance by ID.

## Tab commands

- `odda tabs list [--browser-id <id>]` — List tabs grouped by browser. Use `--browser-id` to filter to one browser.
- `odda switch-tab --browser-id <id> --index <n>` — Switch the active tab. Tab indices come from `odda tabs list`.

## Navigation and page interaction

- `odda navigate <url> [--new-tab]` — Navigate the active browser. If no browser is active, one is opened automatically and reported in the `auto_opened` field.
- `odda eval "<js>"` — Execute JavaScript in the active tab and return the result.
- `odda screenshot` — Capture a JPEG screenshot. Returns the path to the temp file.
- `odda event-listeners` — List JavaScript event listeners attached to `window` and `document`.

## Proxy and flow capture

- `odda proxy-url` — Return the HTTP proxy URL as plain text. Route HTTP clients through this URL to capture traffic.

Captured flows are stored as read-only files under `.odda/flows/`.

### Flow file layout

```
.odda/flows/
├── flows.jsonl                # append-only index, one JSON line per completed/errored flow
└── <NNNNN>/                   # zero-padded monotonic flow id (e.g. 00001)
    ├── request                # reconstructed HTTP request (request line + headers + blank line + decoded body), CRLF
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
  "host": "example.com",
  "path": "/",
  "status_code": 200,
  "total_duration_ms": 12.3,
  "body_file": "flows/00042/response_body.json",
  "error": null
}
```

- `id` — zero-padded flow id matching the directory name; lets you re-sort by capture order with `sort`.
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
