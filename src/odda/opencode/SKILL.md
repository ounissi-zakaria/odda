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
| List captured flows         | `odda flows list [--n N]`                                |
| Query flows with SQL        | `odda flows search "SELECT ..."`                         |
| Inspect one flow            | `odda flows inspect <id>`                                |
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

## Proxy and flow commands

- `odda proxy-url` — Return the HTTP proxy URL as plain text. Route HTTP clients through this URL to capture traffic.
- `odda flows list [--n N]` — Return the latest captured HTTP flows. Default `--n` is 10.
- `odda flows search "<SELECT ...>"` — Query captured flows with a read-only SQL SELECT.
- `odda flows inspect <id>` — Return full request/response details for one flow.

Captured request and response metadata is stored in `.odda/flows.db`. Response bodies are written to `.odda/bodies/` and can be read directly with filesystem tools.

## Server commands

- `odda status` — Show server status, including socket path, data directory, proxy URL, and open browser count.
- `odda logs [--follow] [--n N]` — Show or tail the server log. Default `--n` is 50.
- `odda version` — Print the odda version.

`odda server` and `odda install-opencode` exist but are normally handled by the plugin and the package installer, not by agents at runtime.
