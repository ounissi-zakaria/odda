---
prepend:
  - _lib/boot.md
  - _lib/fixture-server.md
  - _lib/browser-fixture.md
append:
  - _lib/teardown.md
---

# Userscripts and the dialog interceptor

Userscripts are JS helpers that auto-run at `document_start` on every
navigation. `odda` also ships a built-in dialog interceptor that
captures `window.print`/`alert`/`confirm`/`prompt` calls in
`window.__oddaDialogs`.

## Set up the fixture server and browser

```scrut
$ setup_fixture_site index.html dialogs.html
```

```scrut {detached: true, detached_kill_signal: term}
$ port=$(cat "$PWD/fixture_port"); ( python3 -m http.server "$port" --bind 127.0.0.1 --directory "$PWD/site" >"$PWD/http.log" 2>&1 < /dev/null & )
```

```scrut
$ wait_for_fixture_server
```

```scrut
$ open_browser_fixture /
```

## `userscript install` registers a userscript on disk

```scrut
$ printf 'if (!window.__usHelperRan__) window.__usHelperRan__ = 0;\nwindow.__usHelperRan__ += 1;\n' \
>   > "$PWD/us_helper.js"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   userscript install --name helper --browser-id 1 --file "$PWD/us_helper.js" \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["name"], d["size"] > 0)'
helper True
```

## `userscript list` returns the installed script

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" userscript list \
>   | python3 -c 'import json,sys; print([s["name"] for s in json.load(sys.stdin)])'
['helper']
```

## The userscript runs at `document_start` on every navigation

The script increments a counter on every page load. After a navigate
the counter should be `1` (the script is idempotent — it sets the
counter to 0 if absent, then increments, so every navigate leaves it
at `1`).

```scrut
$ navigate_fixture / "window.__usHelperRan__ !== undefined"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__usHelperRan__)" --browser-id 1 --tab-id 1
"1"
```

Navigating again should still leave the counter at `1` — the
userscript re-runs on every navigation but the counter is reset on
each fresh page.

```scrut
$ navigate_fixture / "window.__usHelperRan__ !== undefined"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__usHelperRan__)" --browser-id 1 --tab-id 1
"1"
```

## The dialog interceptor is installed by default

`window.__oddaDialogInterceptorInstalled` is exposed by the default
dialog interceptor userscript.

```scrut
$ navigate_fixture /dialogs.html "window.__oddaDialogInterceptorInstalled"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__oddaDialogInterceptorInstalled)" --browser-id 1 --tab-id 1
"true"
```

### `window.print()` does not block

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "window.print(); 'print-ok'" --browser-id 1 --tab-id 1
"print-ok"
```

### `alert`/`confirm`/`prompt` are captured into `__oddaDialogs`

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "window.alert('alert-msg'); 'alert-ok'" --browser-id 1 --tab-id 1
"alert-ok"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "window.confirm('confirm-msg'); 'confirm-ok'" --browser-id 1 --tab-id 1
"confirm-ok"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "window.prompt('prompt-msg', 'prompt-default'); 'prompt-ok'" --browser-id 1 --tab-id 1
"prompt-ok"
```

The last four dialog entries (in order) should be `print`, `alert`,
`confirm`, `prompt` with the expected messages.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval 'JSON.stringify(window.__oddaDialogs.slice(-4).map(e => [e.type, e.message, e.defaultValue]))' \
>   --browser-id 1 --tab-id 1
"[[\"print\",null,null],[\"alert\",\"alert-msg\",null],[\"confirm\",\"confirm-msg\",null],[\"prompt\",\"prompt-msg\",\"prompt-default\"]]"
```

## `userscript remove` deletes the script and reloads the extension

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   userscript remove helper --browser-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["name"], d["removed"])'
helper True
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" userscript list \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin))'
[]
```

After removing and navigating, the helper variable should be `undefined`.

```scrut
$ navigate_fixture /
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(typeof window.__usHelperRan__)" --browser-id 1 --tab-id 1
"undefined"
```

## Teardown: stop the fixture server

```scrut
$ stop_fixture_server
```