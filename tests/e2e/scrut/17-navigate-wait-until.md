---
prepend:
  - _lib/boot.md
  - _lib/fixture-server.md
  - _lib/browser-fixture.md
append:
  - _lib/teardown.md
---

# navigate: --timeout and --wait-until for SPAs that defer `load`

`odda navigate` used to block for Playwright's default 30s when a
page's `load` event was delayed (e.g. by a slow sub-resource), even
though the page content was ready immediately. The `--timeout` and
`--wait-until` flags let the agent give up fast (`--timeout 3`) or
switch to a lifecycle event that fires earlier (`--wait-until
domcontentloaded`).

This doc reproduces the SPA slow-load scenario: the page's inline JS
runs at `DOMContentLoaded` (sets `window.__spaReady` and writes to
`#app`), but a slow image delays `window.onload` by 35s.

## Set up the slow-load fixture server

A custom stdlib server serves the SPA page plus `/slow-image?sleep=35`,
which sleeps 35s before returning a 1x1 PNG. A threading server keeps
the slow image on one thread from blocking other requests. The fixture
HTML and server script come from the repo's fixture directory;
`setup_fixture_site` copies the HTML into `$PWD/site` and picks a port.

```scrut
$ setup_fixture_site spa-slow-load.html
```

```scrut {detached: true, detached_kill_signal: term}
$ port=$(cat "$PWD/fixture_port"); ( python3 "$TESTDIR/fixtures/slow_load_server.py" "$port" "$PWD/site" >"$PWD/http.log" 2>&1 < /dev/null & )
```

```scrut
$ wait_for_fixture_server
```

## Open a browser

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" browser open --headless > /dev/null
```

## `navigate --help` lists `--timeout` and `--wait-until`

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" navigate --help | grep -E -- '--timeout|--wait-until' | wc -l | tr -d ' '
5
```

## `--timeout 3` errors (not 30s) and names the `load` event

Against the slow-load page, the default `wait-until load` waits for
`window.onload`, which is blocked 35s by the slow image. With
`--timeout 3`, navigate gives up in ~3s. The error still appends the
`wait-for` hint and names the `load` event that timed out so the agent
can pick `domcontentloaded` on retry.

```scrut {output_stream: stderr, timeout: 15s}
$ port=$(cat "$PWD/fixture_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate --url "http://127.0.0.1:$port/spa-slow-load.html" \
>   --browser-id 1 --tab-id 1 --timeout 3
[1]
Error: Failed to navigate: *Timeout* (glob)
Call log:
  - navigating to* (glob)
 (wait-until `load`) *wait-for* (glob)
```

The wall-clock elapsed is well under 30s — `--timeout 3` bounded the
hang. Redirect stderr to a file and assert the elapsed time is under
~12s (3s timeout + Playwright/CDP overhead, generous bound) and the
exit code is 1.

```scrut {timeout: 15s}
$ port=$(cat "$PWD/fixture_port"); start=$(date +%s); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate --url "http://127.0.0.1:$port/spa-slow-load.html" \
>   --browser-id 1 --tab-id 1 --timeout 3 >"$PWD/nav.err" 2>&1; rc=$?; end=$(date +%s); \
>   elapsed=$((end-start)); echo "elapsed=$elapsed rc=$rc"; test "$elapsed" -lt 12 -a "$rc" -eq 1
elapsed=* rc=1 (glob)
```

## `--wait-until domcontentloaded` succeeds (page ready at DOM ready)

The SPA's inline JS runs at `DOMContentLoaded`, so
`--wait-until domcontentloaded` resolves immediately even though
`load` is blocked 35s by the slow image. After navigating, `eval`
confirms the app code already ran.

```scrut {timeout: 15s}
$ port=$(cat "$PWD/fixture_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate --url "http://127.0.0.1:$port/spa-slow-load.html" \
>   --browser-id 1 --tab-id 1 --wait-until domcontentloaded
Navigated to: http://127.0.0.1:* (glob)
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "({ready: window.__spaReady, text: document.getElementById('app').textContent})" \
>   --browser-id 1 --tab-id 1
{"ready": true, "text": "SPA ready at *"} (glob)
```

## `--wait-until commit` resolves once the navigation response is received

`commit` fires when the network response is received and the document
starts loading — earlier than `domcontentloaded`. It also succeeds
immediately against the slow-load page.

```scrut {timeout: 15s}
$ port=$(cat "$PWD/fixture_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate --url "http://127.0.0.1:$port/spa-slow-load.html" \
>   --browser-id 1 --tab-id 1 --wait-until commit
Navigated to: http://127.0.0.1:* (glob)
```

## `--wait-until networkidle` times out and names `networkidle`

`networkidle` waits for 500ms with no network activity, but the slow
image keeps a connection open for 35s. With `--timeout 3` it errors
and names the `networkidle` event.

```scrut {output_stream: stderr, timeout: 15s}
$ port=$(cat "$PWD/fixture_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate --url "http://127.0.0.1:$port/spa-slow-load.html" \
>   --browser-id 1 --tab-id 1 --wait-until networkidle --timeout 3
[1]
Error: Failed to navigate: *Timeout* (glob)
Call log:
  - navigating to* (glob)
 (wait-until `networkidle`) *wait-for* (glob)
```

## `--wait-until` rejects an invalid event

Only the documented lifecycle events are accepted (`commit`,
`domcontentloaded`, `load`, `networkidle`).

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate --url "about:blank" --browser-id 1 --tab-id 1 --wait-until bogus
[1]
Error: *wait_until* (glob)
```

## `--json` mode names the wait-until event in the error

In `--json` mode, errors print as `{"error": ...}` on stdout. The
message includes the `wait-until` event that timed out.

```scrut {timeout: 15s}
$ port=$(cat "$PWD/fixture_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   navigate --url "http://127.0.0.1:$port/spa-slow-load.html" \
>   --browser-id 1 --tab-id 1 --wait-until load --timeout 3
[1]
{"error": "*wait-until `load`*"} (glob)
```

## Default behavior (no flags) matches `--timeout 30 --wait-until load`

The help text reports the default timeout (30s) and default wait-until
event (`load`) so existing scripts keep the 30s `load` behavior. Assert
both defaults appear in the help output.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" navigate --help | grep -E -- 'default: 30\.0|\[default: load\]' | wc -l | tr -d ' '
2
```

## Teardown: stop the fixture server

```scrut
$ pkill -f "slow_load_server.py $PWD" 2>/dev/null || true
```

```scrut
$ for i in $(seq 1 100); do pgrep -f "slow_load_server.py $PWD" >/dev/null || exit 0; sleep 0.05; done; echo "slow_load_server still running" >&2; exit 1
```