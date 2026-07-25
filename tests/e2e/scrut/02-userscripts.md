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

Text output is `name`/`size`/`extension_id` lines; assert the name and
that size is positive.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   userscript install --name helper --browser-id 1 --file "$PWD/us_helper.js" \
>   | grep -q '^name: helper$' && awk '/^size:/ {exit ($2 > 0 ? 0 : 1)}' && echo ok
ok
```

### `userscript install --source` installs inline JS

`--source "<js>"` is the inline alternative to `--file`; the two are
mutually exclusive.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   userscript install --name inline --browser-id 1 --source "window.__usInline__ = 'inline-ran';" \
>   | grep -q '^name: inline$' && awk '/^size:/ {exit ($2 > 0 ? 0 : 1)}' && echo ok
ok
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate --url "http://127.0.0.1:$(cat "$PWD/fixture_port")/" --browser-id 1 --tab-id 1 > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wait-for --expression "window.__usInline__" --browser-id 1 --tab-id 1 --timeout 3
inline-ran
```

Remove the inline script so it doesn't interfere with later tests.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   userscript remove --name inline --browser-id 1 > /dev/null
```

### `userscript install` with both `--file` and `--source` is rejected

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   userscript install --name both --browser-id 1 --file "$PWD/us_helper.js" --source "1"
[1]
Error: Provide either --file or --source, not both
```

### `userscript install` on a missing browser errors

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   userscript install --name x --browser-id 9999 --source "1"
[1]
Error: Browser 9999 not found.
```

### `userscript list` on a missing browser returns an empty list

The browser doesn't exist, so no userscripts were ever installed into
its scope; the list is empty (not an error, since `list` is a read).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   userscript list --browser-id 9999
(no userscripts)
```

## `userscript list` returns the installed script

Text output is a `name  size` table; `--json` here so the test can
pluck the name structurally.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json userscript list --browser-id 1 \
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
>   eval --js "String(window.__usHelperRan__)" --browser-id 1 --tab-id 1
1
```

Navigating again should still leave the counter at `1` — the
userscript re-runs on every navigation but the counter is reset on
each fresh page.

```scrut
$ navigate_fixture / "window.__usHelperRan__ !== undefined"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "String(window.__usHelperRan__)" --browser-id 1 --tab-id 1
1
```

## The dialog interceptor is installed by default

`window.__oddaDialogInterceptorInstalled` is exposed by the default
dialog interceptor userscript.

```scrut
$ navigate_fixture /dialogs.html "window.__oddaDialogInterceptorInstalled"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "String(window.__oddaDialogInterceptorInstalled)" --browser-id 1 --tab-id 1
true
```

### `window.print()` does not block

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "window.print(); 'print-ok'" --browser-id 1 --tab-id 1
print-ok
```

### `alert`/`confirm`/`prompt` are captured into `__oddaDialogs`

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "window.alert('alert-msg'); 'alert-ok'" --browser-id 1 --tab-id 1
alert-ok
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "window.confirm('confirm-msg'); 'confirm-ok'" --browser-id 1 --tab-id 1
confirm-ok
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "window.prompt('prompt-msg', 'prompt-default'); 'prompt-ok'" --browser-id 1 --tab-id 1
prompt-ok
```

The last four dialog entries (in order) should be `print`, `alert`,
`confirm`, `prompt` with the expected messages. The JS returns a
`JSON.stringify`'d string, which text mode prints raw (no outer
quotes, no escaped inner quotes).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js 'JSON.stringify(window.__oddaDialogs.slice(-4).map(e => [e.type, e.message, e.defaultValue]))' \
>   --browser-id 1 --tab-id 1
[["print",null,null],["alert","alert-msg",null],["confirm","confirm-msg",null],["prompt","prompt-msg","prompt-default"]]
```

### `confirm` and `prompt` proceed by default (ADR-0011)

When no response is pre-registered, `confirm` returns `true` and
`prompt` returns `"odda"` so the page proceeds instead of being
silently denied. Re-navigate to reset `__oddaDialogs` first.

```scrut
$ navigate_fixture /dialogs.html "window.__oddaDialogInterceptorInstalled"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "String(window.confirm('are-you-sure'))" --browser-id 1 --tab-id 1
true
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "String(window.prompt('answer-please'))" --browser-id 1 --tab-id 1
odda
```

The recorded `result` for those two entries should match the defaults.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js 'JSON.stringify(window.__oddaDialogs.slice(-2).map(e => [e.type, e.message, e.result]))' \
>   --browser-id 1 --tab-id 1
[["confirm","are-you-sure",true],["prompt","answer-please","odda"]]
```

### Pre-registered responses override the defaults via `__oddaDialogResponses`

The agent sets `window.__oddaDialogResponses` (a per-type map) before
the triggering call; the interceptor returns the registered value
instead of the default, and records it as `result`. Re-navigate to
reset `__oddaDialogs` and the map (fresh `window`) first.

```scrut
$ navigate_fixture /dialogs.html "window.__oddaDialogInterceptorInstalled"
```

Register a prompt response and a confirm denial, then call both.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "window.__oddaDialogResponses = {prompt: 's3cr3t', confirm: false}; 'set'" \
>   --browser-id 1 --tab-id 1
set
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "String(window.prompt('answer-please'))" --browser-id 1 --tab-id 1
s3cr3t
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "String(window.confirm('are-you-sure'))" --browser-id 1 --tab-id 1
false
```

The recorded `result` entries reflect the registered values, not the
defaults.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js 'JSON.stringify(window.__oddaDialogs.slice(-2).map(e => [e.type, e.message, e.result]))' \
>   --browser-id 1 --tab-id 1
[["prompt","answer-please","s3cr3t"],["confirm","are-you-sure",false]]
```

### Registered values pass through verbatim (no type coercion)

A string registered for `confirm` is returned as that string, not
coerced to a boolean — the agent owns the type. Re-navigate to reset
state, register a non-boolean, and confirm the verbatim return.

```scrut
$ navigate_fixture /dialogs.html "window.__oddaDialogInterceptorInstalled"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "window.__oddaDialogResponses = {confirm: 'yes'}; 'set'" \
>   --browser-id 1 --tab-id 1
set
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "String(window.confirm('are-you-sure'))" --browser-id 1 --tab-id 1
yes
```

The recorded `result` is the string `"yes"`, not `true`.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js 'JSON.stringify(window.__oddaDialogs.slice(-1).map(e => [e.type, e.message, e.result]))' \
>   --browser-id 1 --tab-id 1
[["confirm","are-you-sure","yes"]]
```

### `alert`/`print` keys in the response map are ignored

`alert` and `print` have no return value to influence, so the
interceptor ignores keys for those types — they neither throw nor
change behavior. Re-navigate, register both keys, and confirm
`alert`/`print` still behave as before.

```scrut
$ navigate_fixture /dialogs.html "window.__oddaDialogInterceptorInstalled"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "window.__oddaDialogResponses = {alert: 'foo', print: 'bar'}; 'set'" \
>   --browser-id 1 --tab-id 1
set
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "window.alert('alert-msg'); 'alert-ok'" --browser-id 1 --tab-id 1
alert-ok
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "window.print(); 'print-ok'" --browser-id 1 --tab-id 1
print-ok
```

## `userscript remove` deletes the script and reloads the extension

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   userscript remove --name helper --browser-id 1
name: helper
removed: true
extension_id: * (glob)
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" userscript list --browser-id 1
(no userscripts)
```

After removing and navigating, the helper variable should be `undefined`.

```scrut
$ navigate_fixture /
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "String(typeof window.__usHelperRan__)" --browser-id 1 --tab-id 1
undefined
```

## Teardown: stop the fixture server

```scrut
$ stop_fixture_server
```