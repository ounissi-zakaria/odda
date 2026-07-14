---
prepend:
  - _lib/setup.md
---

# Userscripts and the dialog interceptor

Userscripts are JS helpers that auto-run at `document_start` on every
navigation. `odda` also ships a built-in dialog interceptor that
captures `window.print`/`alert`/`confirm`/`prompt` calls in
`window.__oddaDialogs`.

## Boot the server

```scrut {detached: true, detached_kill_signal: term}
$ ( "$ODDA_BIN" server --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   >"$PWD/server.log" 2>&1 & )
```

```scrut {wait: {timeout: 10s, path: "odda.sock"}}
$ echo "server is up"
server is up
```

## Helper: spin up a tiny local HTTP server

```scrut {detached: true, detached_kill_signal: term}
$ ( mkdir -p "$PWD/site" && \
>   cp "$TESTDIR/fixtures/index.html" "$PWD/site/index.html" && \
>   cp "$TESTDIR/fixtures/dialogs.html" "$PWD/site/dialogs.html" && \
>   python3 -m http.server 8766 --bind 127.0.0.1 --directory "$PWD/site" \
>     >"$PWD/http.log" 2>&1 & )
```

```scrut
$ for i in $(seq 1 30); do curl -s -o /dev/null http://127.0.0.1:8766/ && exit 0; sleep 0.5; done; exit 1
```

## Set up a browser

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" browser open --headless \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["browser_id"], d["tab_id"])'
1 1
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://127.0.0.1:8766/ --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: http://127.0.0.1:8766/
```

## `userscript install` registers a userscript on disk

```scrut
$ printf 'if (!window.__usHelperRan__) window.__usHelperRan__ = 0;\nwindow.__usHelperRan__ += 1;\n' \
>   > "$PWD/us_helper.js"
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   userscript install --name helper --browser-id 1 --file "$PWD/us_helper.js" \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["name"], d["size"] > 0)'
helper True
```

## `userscript list` returns the installed script

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" userscript list \
>   | python3 -c 'import json,sys; print([s["name"] for s in json.load(sys.stdin)])'
['helper']
```

## The userscript runs at `document_start` on every navigation

The script increments a counter on every page load. After a navigate
the counter should be `1` (the script is idempotent — it sets the
counter to 0 if absent, then increments, so every navigate leaves it
at `1`).

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://127.0.0.1:8766/ --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: http://127.0.0.1:8766/
```

```scrut
$ sleep 1
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__usHelperRan__)" --browser-id 1 --tab-id 1
"1"
```

Navigating again should still leave the counter at `1` — the
userscript re-runs on every navigation but the counter is reset on
each fresh page.

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://127.0.0.1:8766/ --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: http://127.0.0.1:8766/
```

```scrut
$ sleep 1
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__usHelperRan__)" --browser-id 1 --tab-id 1
"1"
```

## The dialog interceptor is installed by default

`window.__oddaDialogInterceptorInstalled` is exposed by the default
dialog interceptor userscript.

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://127.0.0.1:8766/dialogs.html --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: http://127.0.0.1:8766/dialogs.html
```

```scrut
$ sleep 1
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__oddaDialogInterceptorInstalled)" --browser-id 1 --tab-id 1
"true"
```

### `window.print()` does not block

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "window.print(); 'print-ok'" --browser-id 1 --tab-id 1
"print-ok"
```

### `alert`/`confirm`/`prompt` are captured into `__oddaDialogs`

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "window.alert('alert-msg'); 'alert-ok'" --browser-id 1 --tab-id 1
"alert-ok"
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "window.confirm('confirm-msg'); 'confirm-ok'" --browser-id 1 --tab-id 1
"confirm-ok"
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "window.prompt('prompt-msg', 'prompt-default'); 'prompt-ok'" --browser-id 1 --tab-id 1
"prompt-ok"
```

The last four dialog entries (in order) should be `print`, `alert`,
`confirm`, `prompt` with the expected messages.

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval 'JSON.stringify(window.__oddaDialogs.slice(-4).map(e => [e.type, e.message, e.defaultValue]))' \
>   --browser-id 1 --tab-id 1
"[[\"print\",null,null],[\"alert\",\"alert-msg\",null],[\"confirm\",\"confirm-msg\",null],[\"prompt\",\"prompt-msg\",\"prompt-default\"]]"
```

## `userscript remove` deletes the script and reloads the extension

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   userscript remove helper --browser-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["name"], d["removed"])'
helper True
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" userscript list \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin))'
[]
```

After removing and navigating, the helper variable should be `undefined`.

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://127.0.0.1:8766/ --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: http://127.0.0.1:8766/
```

```scrut
$ sleep 1
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(typeof window.__usHelperRan__)" --browser-id 1 --tab-id 1
"undefined"
```

## Teardown: stop the local HTTP server

The local HTTP server was started in a subshell earlier; kill it
explicitly here so the test doc finishes cleanly.

```scrut
$ pkill -f "http.server 8766.*$PWD/site" 2>/dev/null
```

```scrut
$ sleep 1
```

```scrut
$ pgrep -f "http.server 8766.*$PWD/site" >/dev/null && echo "still running" || echo "stopped"
stopped
```

## Teardown: stop the odda server

```scrut
$ pkill -f "odda.*--data-dir $PWD/data" 2>/dev/null || true
```
