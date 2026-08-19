## Dyn server helpers

Defines `setup_dyn_server`, `wait_for_dyn_server`, and `stop_dyn_server`
for test documents that need a local HTTPS server speaking HTTP/1.1
and HTTP/2 over ALPN with dynamic `?body=&status=&header=&gzip=1`
responses (the same contract as the remote `xs2.top` server). Each test
document that needs the dyn server prepends this file, calls
`setup_dyn_server` in a normal scrut block, then launches the server in
a `detached: true` block using the port written to `$PWD/dyn_port`.

The server is [hypercorn](https://hypercorn.readthedocs.io/) (a dev
dependency) serving the ASGI app at `tests/e2e/scrut/fixtures/dyn_asgi.py`.
`setup_dyn_server` copies the ASGI app into `$PWD/dyn_asgi.py` and
generates a self-signed cert via `openssl req -x509` (CN=127.0.0.1).

The server launch MUST be inline in the `detached: true` block (not
inside a function call) — scrut kills the process group of function
calls in detached blocks, but preserves inline `( ... & )` subshells
until the document ends.

`pick_port` is provided by `_lib/fixture-server.md`; prepend that file
before this one if the document does not already include it.

```scrut
$ setup_dyn_server() {
>   pick_port > "$PWD/dyn_port"
>   cp "$TESTDIR/fixtures/dyn_asgi.py" "$PWD/dyn_asgi.py"
>   openssl req -x509 -newkey rsa:2048 \
>     -keyout "$PWD/dyn.key" -out "$PWD/dyn.pem" \
>     -days 1 -nodes -subj "/CN=127.0.0.1" 2>/dev/null
> }
```

Wait for the dyn server (launched in the preceding detached block) to
answer on `$PWD/dyn_port`. Bounded poll, fails fast on success.

```scrut
$ wait_for_dyn_server() {
>   port=$(cat "$PWD/dyn_port")
>   for i in $(seq 1 500); do curl -s -k -o /dev/null "https://127.0.0.1:$port/" && exit 0; sleep 0.03; done
>   echo "dyn server on $port not reachable" >&2
>   exit 1
> }
```

Stop the dyn server with a bounded process-absence poll. Matches on the
cert path so only this document's server is killed.

```scrut
$ stop_dyn_server() {
>   [ -f "$PWD/dyn_port" ] || return 0
>   pkill -f "hypercorn.*$PWD/dyn.pem" 2>/dev/null || true
>   for i in $(seq 1 500); do pgrep -f "hypercorn.*$PWD/dyn.pem" >/dev/null || exit 0; sleep 0.03; done
>   echo "dyn server still running" >&2
>   exit 1
> }
```