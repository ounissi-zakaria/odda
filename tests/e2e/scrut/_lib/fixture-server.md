## Fixture server helpers

Defines `setup_fixture_site`, `wait_for_fixture_server`,
`stop_fixture_server`, `wait_for_flow`, and `pick_port`. Each test
document that needs a local HTTP server prepends this file, calls
`setup_fixture_site` with the fixture filenames in a normal scrut
block, then launches the server in a `detached: true` block using the
port written to `$PWD/fixture_port`.

The port is auto-picked (with a retry loop in the launch block) and
written to `$PWD/fixture_port` by `setup_fixture_site`. Other helpers
(`open_browser_fixture`, `navigate_fixture`, `stop_fixture_server`)
read the port from that file.

`pick_port` is a standalone helper for docs that need a free port for
a non-standard server (e.g. the self-signed HTTPS server in
`08-request-send-extra.md`).

The server launch MUST be inline in the `detached: true` block (not
inside a function call) — scrut kills the process group of function
calls in detached blocks, but preserves inline `( ... & )` subshells
until the document ends.

```scrut
$ pick_port() {
>   python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
> }
```

```scrut
$ setup_fixture_site() {
>   mkdir -p "$PWD/site"
>   for f in "$@"; do cp "$TESTDIR/fixtures/$f" "$PWD/site/"; done
>   pick_port > "$PWD/fixture_port"
> }
```

Wait for the fixture server (launched in the preceding detached
block) to answer on `$PWD/fixture_port`. Bounded poll, fails fast on
success.

```scrut
$ wait_for_fixture_server() {
>   port=$(cat "$PWD/fixture_port")
>   for i in $(seq 1 250); do curl -s -o /dev/null "http://127.0.0.1:$port/" && exit 0; sleep 0.02; done
>   echo "fixture server on $port not reachable" >&2
>   exit 1
> }
```

Stop the fixture server with a bounded process-absence poll. Reads
the port from `$PWD/fixture_port`.

```scrut
$ stop_fixture_server() {
>   [ -f "$PWD/fixture_port" ] || return 0
>   port=$(cat "$PWD/fixture_port")
>   pkill -f "http.server $port.*$PWD/site" 2>/dev/null || true
>   for i in $(seq 1 250); do pgrep -f "http.server $port.*$PWD/site" >/dev/null || exit 0; sleep 0.02; done
>   echo "fixture server still running on $port" >&2
>   exit 1
> }
```

A bounded poll for a captured flow to appear in `flows.jsonl`, keyed
on a unique URL marker (not just the host, which can match stale or
browser-background flows). Use after `curl`-through-the-proxy to wait
for mitmproxy to flush the flow to disk.

```scrut
$ wait_for_flow() {
>   marker="$1"
>   for i in $(seq 1 250); do grep -q "$marker" "$PWD/data/flows/flows.jsonl" 2>/dev/null && exit 0; sleep 0.02; done
>   echo "flow matching $marker not flushed to disk" >&2
>   exit 1
> }
```