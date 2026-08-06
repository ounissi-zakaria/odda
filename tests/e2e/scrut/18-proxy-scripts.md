---
prepend:
  - _lib/boot.md
  - _lib/fixture-server.md
append:
  - _lib/teardown.md
---

# Proxy-scripts (user-supplied mitmproxy addons)

A proxy-script is a Python file in mitmproxy `-s` script format that
odda execs and adds to the running proxy's addon chain. Installed via
`odda proxy-script install`, persisted under
`$data_dir/proxy-scripts/<name>/script.py`, re-added on server boot.
Scope is global: one proxy shared across all browsers, so a proxy-script
sees every flow. `--name` is odda's key; `--force` gates overwrite.

See ADR-0018 and CONTEXT.md "Proxy interception" for the design.

## Set up the fixture server

A fixture server is needed so proxy-scripts with `request`/`response`
hooks have real traffic to act on later in this document.

```scrut
$ mkdir -p "$PWD/site" && \
>   echo "<html><body>odda-proxy-script-marker</body></html>" > "$PWD/site/index.html"
```

```scrut
$ pick_port > "$PWD/fixture_port"
```

```scrut {detached: true, detached_kill_signal: term}
$ port=$(cat "$PWD/fixture_port"); ( python3 -m http.server "$port" --bind 127.0.0.1 --directory "$PWD/site" >"$PWD/http.log" 2>&1 < /dev/null & )
```

```scrut
$ wait_for_fixture_server
```

## `proxy-script install` stores the source on disk

A minimal mitmproxy script: top-level `response` hook (module namespace
is the addon, per mitmproxy `-s` semantics). This one is a no-op so the
lifecycle test doesn't depend on hook firing (that's covered later).

```scrut
$ printf 'def response(flow):\n    pass\n' > "$PWD/ps_minimal.py"
```

Text output is `name`/`size` lines; assert the name and that size is
positive.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script install --name minimal --file "$PWD/ps_minimal.py" \
>   | grep -q '^name: minimal$' && awk '/^size:/ {exit ($2 > 0 ? 0 : 1)}' && echo ok
ok
```

The source is persisted under the data dir.

```scrut
$ test -f "$PWD/data/proxy-scripts/minimal/script.py" && echo "persisted"
persisted
```

### `proxy-script install --source` installs inline Python

`--source "<py>"` is the inline alternative to `--file`; the two are
mutually exclusive. A single-line function body is valid Python.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script install --name inline --source "def response(flow): pass" \
>   | grep -q '^name: inline$' && awk '/^size:/ {exit ($2 > 0 ? 0 : 1)}' && echo ok
ok
```

```scrut
$ test -f "$PWD/data/proxy-scripts/inline/script.py" && echo "persisted"
persisted
```

## `proxy-script list` returns the installed scripts

Text output is a `name  size` table; `--json` so the test can pluck the
names structurally.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json proxy-script list \
>   | python3 -c 'import json,sys; names=sorted(s["name"] for s in json.load(sys.stdin)); print(names)'
['inline', 'minimal']
```

## `proxy-script remove` deletes the script and removes it from the live chain

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script remove --name inline
name: inline
removed: true
```

```scrut
$ test ! -e "$PWD/data/proxy-scripts/inline" && echo "gone"
gone
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json proxy-script list \
>   | python3 -c 'import json,sys; print([s["name"] for s in json.load(sys.stdin)])'
['minimal']
```

## `proxy-script remove` on a missing name errors

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" proxy-script remove --name nope
[1]
Error: Proxy-script 'nope' not found
```

## The proxy-script actually runs in the proxy

A proxy-script with a `response` hook that adds a response header.
curl through odda's proxy and observe the header — this proves exec +
add + RunningHook + hook firing all work end-to-end on real traffic.

```scrut
$ printf 'def response(flow):\n    flow.response.headers["x-odda-proxy-script"] = "fired"\n' \
>   > "$PWD/ps_addheader.py"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script install --name addheader --file "$PWD/ps_addheader.py" \
>   | grep -q '^name: addheader$' && echo ok
ok
```

Send one request through the proxy and check the injected header is
present in the response. Use a unique query parameter so the flow is
identifiable later.

```scrut
$ port=$(cat "$PWD/fixture_port"); curl -s -D - -x "$(odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" proxy-url)" \
>   "http://127.0.0.1:$port/?marker=ps-fires" -o /dev/null \
>   | grep -qi 'x-odda-proxy-script: fired' && echo ok
ok
```

### Removing the proxy-script stops the hook from firing

After `remove`, a fresh request through the proxy no longer carries the
injected header.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script remove --name addheader > /dev/null
```

```scrut
$ port=$(cat "$PWD/fixture_port"); curl -s -D - -x "$(odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" proxy-url)" \
>   "http://127.0.0.1:$port/?marker=ps-stopped" -o /dev/null \
>   | grep -qi 'x-odda-proxy-script' && echo "STILL FIRING (bad)" || echo "stopped"
stopped
```

The flow for the first request was still captured to disk (the
proxy-script ran after FlowFileAddon, so capture is unaffected).

```scrut
$ wait_for_flow "ps-fires"
```

```scrut
$ grep -q 'ps-fires' "$PWD/data/flows/flows.jsonl" && echo "captured"
captured
```

## `--force` overwrites an existing proxy-script (name-gated)

`--name` is odda's key. Without `--force`, installing a name that's
already installed errors (same semantics as `request clone`/`new`).

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script install --name minimal --source "def response(flow): pass"
[1]
Error: Proxy-script 'minimal' already installed. Use --force to overwrite.
```

With `--force`, odda removes the existing live instance first, then
execs and adds the new source. Reinstall `addheader` with `--force`
using a *different* header value, then confirm the live chain swapped
(the new value shows up, not the old one).

```scrut
$ printf 'def response(flow):\n    flow.response.headers["x-odda-proxy-script"] = "replaced"\n' \
>   > "$PWD/ps_addheader2.py"
```

`addheader` was removed earlier, so reinstall it fresh first, then
overwrite with `--force` to test the replace path specifically.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script install --name addheader --file "$PWD/ps_addheader.py" > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script install --name addheader --file "$PWD/ps_addheader2.py" --force \
>   | grep -q '^name: addheader$' && echo ok
ok
```

The on-disk source is the new one.

```scrut
$ grep -q 'replaced' "$PWD/data/proxy-scripts/addheader/script.py" && echo "new source on disk"
new source on disk
```

The live chain reflects the replacement: a request through the proxy
gets the new header value, not the old one.

```scrut
$ port=$(cat "$PWD/fixture_port"); curl -s -D - -x "$(odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" proxy-url)" \
>   "http://127.0.0.1:$port/?marker=ps-replaced" -o /dev/null \
>   | grep -qi 'x-odda-proxy-script: replaced' && echo ok
ok
```

Clean up the addheader proxy-script so later slices start clean.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script remove --name addheader > /dev/null
```

## `proxy-script install` with both `--file` and `--source` is rejected

The two are mutually exclusive (mirrors `userscript install`).

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script install --name both --file "$PWD/ps_minimal.py" --source "1"
[1]
Error: Provide either --file or --source, not both
```

## `proxy-script install` with neither `--file` nor `--source` is rejected

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script install --name neither
[1]
Error: Provide --file <path> or --source <py>
```

## `proxy-script install --file` on a missing file errors

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script install --name missing --file "$PWD/does-not-exist.py"
[1]
Error: File not found: * (glob)
```

## `proxy-script install` rejects empty source

Whitespace-only source is rejected by the server (mirrors `userscript
install`'s empty-source guard).

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script install --name empty --source "   "
[1]
Error: source is empty
```

## A runtime hook error is logged and does not crash the proxy

A proxy-script whose `response` hook raises. mitmproxy's `safecall()`
logs the exception and the proxy continues; the agent discovers the
error via `odda logs`. The proxy must keep serving traffic and capturing
flows after the error.

```scrut
$ printf 'def response(flow):\n    raise RuntimeError("ps-boom")\n' > "$PWD/ps_raise.py"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script install --name raiser --file "$PWD/ps_raise.py" \
>   | grep -q '^name: raiser$' && echo ok
ok
```

Send a request through the proxy. The hook raises, but the proxy
still returns a response to the client (mitmproxy continues after
safecall swallows the error).

```scrut
$ port=$(cat "$PWD/fixture_port"); curl -s -o /dev/null -w "curl_status=%{http_code}\n" \
>   -x "$(odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" proxy-url)" \
>   "http://127.0.0.1:$port/?marker=ps-boom"
curl_status=200
```

The error is visible in `odda logs` (mitmproxy logs `Addon error` via
`safecall`).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" logs --n 200 | grep -q 'ps-boom' && echo "logged"
logged
```

The proxy still serves and captures a *subsequent* request — proving
the error didn't crash the proxy or the flow store.

```scrut
$ port=$(cat "$PWD/fixture_port"); curl -s -o /dev/null -w "curl_status=%{http_code}\n" \
>   -x "$(odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" proxy-url)" \
>   "http://127.0.0.1:$port/?marker=ps-after-boom"
curl_status=200
```

```scrut
$ wait_for_flow "ps-after-boom"
```

```scrut
$ grep -q 'ps-after-boom' "$PWD/data/flows/flows.jsonl" && echo "still capturing"
still capturing
```

Clean up the raiser so later slices start clean.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script remove --name raiser > /dev/null
```

## A persisted proxy-script is re-added on server boot

Install a proxy-script, stop the odda server, start a fresh one on the
same data dir, and confirm the boot scan re-execed and re-added it.
Observable two ways: `proxy-script list` shows it, and the hook fires
on traffic through the new server.

Install a header-injecting proxy-script.

```scrut
$ printf 'def response(flow):\n    flow.response.headers["x-odda-restored"] = "yes"\n' \
>   > "$PWD/ps_restore.py"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script install --name restorer --file "$PWD/ps_restore.py" > /dev/null
```

Stop the odda server and wait for the socket to disappear.

```scrut
$ pkill -f "odda.*--data-dir $PWD/data" 2>/dev/null || true
```

```scrut
$ for i in $(seq 1 100); do test -S "$PWD/odda.sock" || exit 0; sleep 0.05; done; exit 1
```

Start a fresh odda server on the same data dir (same as `_lib/boot.md`
but inline, since the boot helper is only prepended once at the top).

```scrut {detached: true, detached_kill_signal: term}
$ nohup odda server --socket "$PWD/odda.sock" --data-dir "$PWD/data" --log "$PWD/server.log" \
>   >"$PWD/server.log" 2>&1 &
```

```scrut
$ for i in $(seq 1 100); do test -S "$PWD/odda.sock" && exit 0; sleep 0.1; done; exit 1
```

The boot scan re-added `restorer`: `proxy-script list` shows it.
(`minimal` from slice 1 is also still installed and restored.)

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json proxy-script list \
>   | python3 -c 'import json,sys; print("restorer" in [s["name"] for s in json.load(sys.stdin)])'
True
```

And the hook fires on traffic through the new server — proving the
addon is live, not just listed.

```scrut
$ port=$(cat "$PWD/fixture_port"); curl -s -D - -x "$(odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" proxy-url)" \
>   "http://127.0.0.1:$port/?marker=ps-restored" -o /dev/null \
>   | grep -qi 'x-odda-restored: yes' && echo ok
ok
```

Clean up the restorer.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script remove --name restorer > /dev/null
```

## Capture honesty: FlowFileAddon records the original, mutations reach upstream

FlowFileAddon is ahead of user proxy-scripts in the addon chain
(ADR-0018), so a proxy-script that mutates `flow.request` in its
`request` hook mutates what goes upstream, but the captured
`.odda/flows/<id>/request` file records the **original** request.

A `request` hook adds a custom header that the client did not send.

```scrut
$ printf 'def request(flow):\n    flow.request.headers["x-odda-injected"] = "upstream"\n' \
>   > "$PWD/ps_inject.py"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script install --name injector --file "$PWD/ps_inject.py" > /dev/null
```

Send a request through the proxy. The injected header reaches the
upstream server — but we can't see the upstream's view directly (the
fixture server doesn't echo headers). So prove the mutation happened
by reading it back from the captured flow's request file: if capture
recorded the *original*, the injected header is **absent** there even
though it reached upstream.

```scrut
$ port=$(cat "$PWD/fixture_port"); curl -s -o /dev/null -w "curl_status=%{http_code}\n" \
>   -x "$(odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" proxy-url)" \
>   "http://127.0.0.1:$port/?marker=ps-capture-honesty"
curl_status=200
```

```scrut
$ wait_for_flow "ps-capture-honesty"
```

```scrut
$ cap_id=$(grep 'ps-capture-honesty' "$PWD/data/flows/flows.jsonl" | head -n 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
```

The captured `request` file does NOT contain the injected header —
capture recorded the original request, not the post-mutation one.

```scrut
$ grep -qi 'x-odda-injected' "$PWD/data/flows/$cap_id/request" \
>   && echo "FAIL: capture saw the mutation" || echo "capture is original"
capture is original
```

### The mutation did reach the upstream

Prove the injected header actually reached the upstream (it wasn't
just dropped) using a `response` hook in a *second* proxy-script that
echoes the mutated request header back to the client. `request` hooks
fire before `response` hooks, so by the time the `response` hook runs,
`flow.request.headers` carries the injection.

```scrut
$ printf 'def response(flow):\n    flow.response.headers["x-odda-echo"] = flow.request.headers.get("x-odda-injected", "missing")\n' \
>   > "$PWD/ps_echo.py"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script install --name echo --file "$PWD/ps_echo.py" > /dev/null
```

```scrut
$ port=$(cat "$PWD/fixture_port"); curl -s -D - -x "$(odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" proxy-url)" \
>   "http://127.0.0.1:$port/?marker=ps-echo" -o /dev/null \
>   | grep -qi 'x-odda-echo: upstream' && echo ok
ok
```

The response carries `x-odda-echo: upstream`, proving the injected
`x-odda-injected: upstream` header was present on `flow.request` when
the request went upstream — the mutation happened, capture just didn't
record it (by design).

Clean up both proxy-scripts.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script remove --name injector > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   proxy-script remove --name echo > /dev/null
```

## Teardown: stop the fixture server

```scrut
$ stop_fixture_server
```