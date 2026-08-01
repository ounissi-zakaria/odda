---
prepend:
  - _lib/boot.md
  - _lib/fixture-server.md
  - _lib/browser-fixture.md
append:
  - _lib/teardown.md
---

# Tab lifecycle, multi-browser, and ID monotonicity

Tab and browser IDs are integers, monotonic, and never reused. A
closed tab's id is retired forever. The browser stays alive with
zero tabs after its last tab is closed. Multiple browsers are
isolated.

## Set up the fixture server

```scrut
$ setup_fixture_site
```

```scrut {detached: true, detached_kill_signal: term}
$ port=$(cat "$PWD/fixture_port"); ( python3 -m http.server "$port" --bind 127.0.0.1 --directory "$PWD/site" >"$PWD/http.log" 2>&1 < /dev/null & )
```

```scrut
$ wait_for_fixture_server
```

## Open a browser with one initial tab

```scrut
$ open_browser_fixture
```

## `tabs list` returns the browser with its initial tab

In text mode `tabs list` prints a table; one browser (id 1) with one
tab (id 1). Count the data rows structurally with `--json` (the
scripted-extraction use case).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json tabs list \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(len(b["tabs"]) for b in d))'
1
```

## `tabs open --url` opens a new tab and returns its id

In text mode `tabs open` prints `browser_id`, `tab_id`, and `status`
as `key: value` lines; assert the new tab id and status directly.

```scrut
$ port=$(cat "$PWD/fixture_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   tabs open --browser-id 1 --url "http://127.0.0.1:$port/" \
>   | grep -E '^(tab_id|status): ' | sed 's/: / /'
tab_id 2
status opened
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json tabs list \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(len(b["tabs"]) for b in d))'
2
```

## `tabs open` without `--url` opens a blank tab

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   tabs open --browser-id 1
browser_id: 1
tab_id: 3
status: opened
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json tabs list \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(len(b["tabs"]) for b in d))'
3
```

## `tabs close` drops the tab count

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   tabs close --browser-id 1 --tab-id 2
browser_id: 1
tab_id: 2
status: closed
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json tabs list \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(len(b["tabs"]) for b in d))'
2
```

## A closed tab's id stays dead — `eval` on it errors

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "1" --browser-id 1 --tab-id 2
[1]
Error: Tab 2 not found in browser 1.
```

## New tab IDs are strictly higher than any previous id

```scrut
$ port=$(cat "$PWD/fixture_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   tabs open --browser-id 1 --url "http://127.0.0.1:$port/"
browser_id: 1
tab_id: 4
status: opened
```

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "1" --browser-id 1 --tab-id 2
[1]
Error: Tab 2 not found in browser 1.
```

(`Tab 2` is still dead — IDs are not reused.)

## Closing all tabs leaves the browser alive with zero tabs

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" tabs close --browser-id 1 --tab-id 1 > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" tabs close --browser-id 1 --tab-id 3 > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" tabs close --browser-id 1 --tab-id 4 > /dev/null
```

With zero tabs, `tabs list --browser-id 1` renders an empty table
(`(no tabs)`). Assert structurally with `--json` to confirm the
single browser has a zero-length tab list.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json tabs list --browser-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d[0]["tabs"]))'
0
```

## A browser with zero tabs can still open new tabs

```scrut
$ port=$(cat "$PWD/fixture_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   tabs open --browser-id 1 --url "http://127.0.0.1:$port/"
browser_id: 1
tab_id: 5
status: opened
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json tabs list --browser-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d[0]["tabs"]))'
1
```

## Multiple browsers are isolated

Close the first browser, then open two more.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" browser close --browser-id 1
browser_id: 1
status: closed
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" browser open
browser_id: 2
tab_id: 1
status: launched
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate --url https://example.org --browser-id 2 --tab-id 1
Navigated to: https://example.org (glob)
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json tabs list \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(len(b["tabs"]) for b in d))'
1
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" browser open
browser_id: 3
tab_id: 1
status: launched
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json tabs list \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d))'
2
```

Operating on browser 2 doesn't affect browser 3. In text mode `eval`
prints strings without quotes.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "document.title" --browser-id 2 --tab-id 1
Example Domain
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" browser close --browser-id 3
browser_id: 3
status: closed
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" browser close --browser-id 2
browser_id: 2
status: closed
```

## A closed browser_id is not reused

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate --url http://x --browser-id 2 --tab-id 1
[1]
Error: Browser 2 not found.
```

## Teardown: stop the fixture server

```scrut
$ stop_fixture_server
```