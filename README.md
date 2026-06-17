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
odda navigate https://example.com
odda screenshot
odda flows search "SELECT * FROM flows WHERE host = 'example.com' LIMIT 10"
```

### Standalone mode

You can also run the server manually:

```bash
odda server --socket /tmp/odda-$$.sock --data-dir ./.odda --parent-pid $$
ODDA_SOCKET=/tmp/odda-$$.sock odda navigate https://example.com
```

## CLI commands

```
odda server              # Run the background server
odda install-opencode    # Install OpenCode plugin and skill
odda status              # Server status
odda logs                # Server logs

odda proxy-url           # HTTP proxy URL

odda browser open        # Open a new Chrome window
odda browser list        # List browser instances
odda browser close       # Close a browser instance

odda navigate            # Navigate active browser
odda eval                # Execute JavaScript
odda screenshot          # Capture JPEG screenshot

odda tabs                # List tabs
odda switch-tab          # Switch tab

odda event-listeners     # List JS event listeners

odda flows list          # Latest captured flows
odda flows search        # Query flows with SQL
odda flows inspect       # Inspect a single flow
```

## Data storage

Captured traffic is stored under `.odda/` in the project directory:

- `flows.db` — SQLite database with request/response metadata
- `bodies/` — response body files
- `server.log` — background server logs

## Chrome profile

To configure a base Chrome profile (cookies, extensions, preferences):

```bash
scripts/init-chrome-profile
```

`odda` copies this profile for each isolated browser session.
