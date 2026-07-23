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

Open a second browser. It gets `browser_id` 2 with its own tab.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" browser open --headless \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["browser_id"] == 2)'
True
```

## `wrap list` on browser 2 starts empty (no leak from browser 1)

Install a wrap on browser 1. Browser 2's `wrap list` must not show it.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap calls add --browser-id 1 --tab-id 1 --expr JSON.parse --name leaktest \
>   > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap list --browser-id 2 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d == [])'
True
```

Browser 1 still has its own wrap.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
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
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap list --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(any(w["name"] == "b2wrap" for w in d))'
False
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
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

Navigate browser 2's tab; the userscript must NOT run there.

```scrut
$ port=$(cat "$PWD/fixture_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate --url "http://127.0.0.1:$port/" --browser-id 2 --tab-id 1 > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wait-for --expression "typeof window.__perBrowserUs__" --browser-id 2 --tab-id 1 --timeout 3 2>&1 \
>   | python3 -c 'import json,sys; raw=sys.stdin.read().strip(); print("undefined" in raw or "error" in raw.lower())'
True
```

Navigate browser 1's tab; the userscript DOES run there.

```scrut
$ port=$(cat "$PWD/fixture_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate --url "http://127.0.0.1:$port/" --browser-id 1 --tab-id 1 > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wait-for --expression "window.__perBrowserUs__" --browser-id 1 --tab-id 1 --timeout 3 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin))'
browser-1
```

## Default userscripts ship in every browser's scope

The dialog interceptor is a default userscript; it must run in both
browser 1 and browser 2 even though it was never "installed" on either.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wait-for --expression "window.__oddaDialogInterceptorInstalled" --browser-id 1 --tab-id 1 --timeout 3 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin))'
True
```

```scrut
$ port=$(cat "$PWD/fixture_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate --url "http://127.0.0.1:$port/" --browser-id 2 --tab-id 1 > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wait-for --expression "window.__oddaDialogInterceptorInstalled" --browser-id 2 --tab-id 1 --timeout 3 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin))'
True
```

## Teardown: stop the fixture server

```scrut
$ stop_fixture_server
```