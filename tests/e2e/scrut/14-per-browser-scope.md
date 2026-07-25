---
prepend:
  - _lib/boot.md
  - _lib/fixture-server.md
  - _lib/browser-fixture.md
append:
  - _lib/teardown.md
---

# Per-browser scope: wraps and userscripts do not leak across browsers

Wraps and userscripts are stored per-browser. A wrap or userscript
installed on browser 1 does not appear in browser 2's `wrap list` or
`userscript list`, and does not run in browser 2's tabs. Each browser
gets its own on-disk userscripts directory and its own userscript
extension instance (per ADR-0010). The `--browser-id` on install/remove
selects the scope; the default userscripts (dialog interceptor) ship
in every browser's scope.

## Set up the fixture server and two browsers

```scrut
$ setup_fixture_site index.html
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

Open a second browser. In text mode `browser open` prints
`browser_id`/`tab_id`/`status` lines; assert the second browser got
id 2.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" browser open --headless \
>   | grep -q '^browser_id: 2$' && echo True
True
```

## `wrap list` on browser 2 starts empty (no leak from browser 1)

Install a wrap on browser 1. Browser 2's `wrap list` must not show it.
In text mode an empty `wrap list` renders as `(no wraps)`.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap calls add --browser-id 1 --tab-id 1 --expr JSON.parse --name leaktest \
>   > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap list --browser-id 2 --tab-id 1
(no wraps)
```

Browser 1 still has its own wrap. `wrap list` is structural (a list of
wrap dicts), so the membership assertion uses `--json` and parses the
list.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   wrap list --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(any(w["name"] == "leaktest" for w in d))'
True
```

## A wrap installed on browser 2 does not appear on browser 1

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap calls add --browser-id 2 --tab-id 1 --expr JSON.parse --name b2wrap \
>   > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   wrap list --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(any(w["name"] == "b2wrap" for w in d))'
False
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   wrap list --browser-id 2 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(any(w["name"] == "b2wrap" for w in d))'
True
```

## `userscript list` is per-browser

Install a userscript on browser 1. Browser 2's `userscript list`
must not show it. We also confirm via behavior: the userscript sets a
marker that should appear in browser 1's tab but not browser 2's.

```scrut
$ printf 'window.__perBrowserUs__ = "browser-1";\n' > "$PWD/us1.js"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   userscript install --name us1 --browser-id 1 --file "$PWD/us1.js" \
>   > /dev/null
```

`userscript list` is structural (a list of userscript dicts), so the
membership assertions use `--json` and parse the list.

`userscript list --browser-id 2` must not include `us1`.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   userscript list --browser-id 2 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(any(s["name"] == "us1" for s in d))'
False
```

`userscript list --browser-id 1` must include `us1`.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   userscript list --browser-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(any(s["name"] == "us1" for s in d))'
True
```

Navigate browser 2's tab; the userscript must NOT run there.
`typeof window.__perBrowserUs__` is the truthy string `"undefined"`
(the variable was never set in browser 2's scope), so `wait-for`
returns it immediately and text mode prints `undefined`.

```scrut
$ port=$(cat "$PWD/fixture_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate --url "http://127.0.0.1:$port/" --browser-id 2 --tab-id 1 > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wait-for --expression "typeof window.__perBrowserUs__" --browser-id 2 --tab-id 1 --timeout 3
undefined
```

Navigate browser 1's tab; the userscript DOES run there. `wait-for`
returns the marker string and text mode prints it unquoted.

```scrut
$ port=$(cat "$PWD/fixture_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate --url "http://127.0.0.1:$port/" --browser-id 1 --tab-id 1 > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wait-for --expression "window.__perBrowserUs__" --browser-id 1 --tab-id 1 --timeout 3
browser-1
```

## Default userscripts ship in every browser's scope

The dialog interceptor is a default userscript; it must run in both
browser 1 and browser 2 even though it was never "installed" on either.
`window.__oddaDialogInterceptorInstalled` is a boolean; `wait-for`
returns it and text mode renders `true` (lowercase, JSON convention).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wait-for --expression "window.__oddaDialogInterceptorInstalled" --browser-id 1 --tab-id 1 --timeout 3
true
```

```scrut
$ port=$(cat "$PWD/fixture_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate --url "http://127.0.0.1:$port/" --browser-id 2 --tab-id 1 > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wait-for --expression "window.__oddaDialogInterceptorInstalled" --browser-id 2 --tab-id 1 --timeout 3
true
```

## Teardown: stop the fixture server

```scrut
$ stop_fixture_server
```