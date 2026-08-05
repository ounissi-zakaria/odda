# odda

Browser automation and HTTP traffic capture CLI for AI agents.

`odda` is composable command-line tool. An OpenCode plugin starts a per-session background server; the agent invokes `odda` commands from bash to drive Chrome and inspect captured network traffic.

## Installation

With `uv` (recommended):

```bash
uv pip install odda
odda install-opencode
```

Or with `pip`:

```bash
pip install odda
odda install-opencode
```

This installs the `odda` CLI and copies the OpenCode plugin + skill into `~/.config/opencode/`.

## Requirements

- Python 3.11+
- Chrome or Chromium installed and on PATH

## Usage

From within an OpenCode session:

```bash
# Open a browser; returns {browser_id, tab_id, status}
odda browser open
# Navigate the initial tab (browser 1, tab 1)
odda navigate https://example.com --browser-id 1 --tab-id 1
odda screenshot --browser-id 1 --tab-id 1
odda flows search "SELECT * FROM flows WHERE host = 'example.com' LIMIT 10"
```

### Standalone mode

You can also run the server manually:

```bash
odda server --socket /tmp/odda-$$.sock --data-dir ./.odda --parent-pid $$
ODDA_SOCKET=/tmp/odda-$$.sock odda browser open
ODDA_SOCKET=/tmp/odda-$$.sock odda navigate https://example.com --browser-id 1 --tab-id 1
```

## CLI commands

```
odda server              # Run the background server
odda install-opencode    # Install OpenCode plugin and skill
odda status              # Server status
odda logs                # Server logs

odda proxy-url           # HTTP proxy URL

odda browser open        # Open a new Chrome window (returns browser_id + initial tab_id)
odda browser close       # Close a browser instance

odda tabs list           # List tabs grouped by browser (overview)
odda tabs open           # Open a new tab (returns tab_id)
odda tabs close          # Close an individual tab

odda navigate            # Navigate an existing tab (--browser-id + --tab-id)
odda eval                # Execute JavaScript (--browser-id + --tab-id)
odda screenshot          # Capture JPEG screenshot (--browser-id + --tab-id)

odda event-listeners     # List JS event listeners (--browser-id + --tab-id)

odda flows list          # Latest captured flows
odda flows search        # Query flows with SQL
odda flows inspect       # Inspect a single flow
```

Every browser/tab command takes an explicit `--browser-id` and tab-scoped
commands also take `--tab-id`. IDs are integers, monotonic, and never reused,
so multiple agents can share one odda server without racing on a shared
cursor.

## Data storage

The data directory (`.odda/` in the project directory, or `--data-dir` /
`ODDA_DATA_DIR`) holds persisted project state — `flows/`, `requests/`,
`browsers/` (including per-browser userscripts). It is created lazily on
the first state-producing command (e.g. `browser open`, `request clone`),
not when the server boots, so opening a session without using odda does
not litter the project dir.

The server's Unix socket and log file are session plumbing: they live in
`$XDG_RUNTIME_DIR` (`odda-<pid>.sock` and `odda-<pid>.log`), not in the
data dir. `odda logs` finds the log via `ODDA_LOG` (set by the OpenCode
plugin) or the server's `status.log_path` field.

## Chrome profile

To configure a base Chrome profile (cookies, extensions, preferences):

```bash
scripts/init-chrome-profile
```

`odda` copies this profile for each isolated browser session.
