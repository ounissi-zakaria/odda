---
prepend:
  - _lib/boot.md
  - _lib/fixture-server.md
  - _lib/browser-fixture.md
append:
  - _lib/teardown.md
---

# Coverage: start, snapshot, and stop block-level hit counts

`odda coverage` exposes three commands — `start`, `snapshot`, `stop` —
that record which code blocks execute during a window of interest.
Coverage is per-tab; starting on one tab does not affect another. The
recording window spans navigations (per ADR-0005): the flag,
accumulator, and Profiler all survive a navigate, so an agent can
`start` → `navigate` → `snapshot`/`stop` to observe code that runs as
a consequence of navigating. Zero-hit blocks are included (the negative
space is as informative as the positive).

## Set up the fixture server and browser

```scrut
$ setup_fixture_site coverage.html
```

```scrut {detached: true, detached_kill_signal: term}
$ port=$(cat "$PWD/fixture_port"); ( python3 -m http.server "$port" --bind 127.0.0.1 --directory "$PWD/site" >"$PWD/http.log" 2>&1 < /dev/null & )
```

```scrut
$ wait_for_fixture_server
```

```scrut
$ open_browser_fixture /coverage.html "typeof window.__oddaCoverageFixture === 'function'"
```

## `coverage start` enables precise block-level coverage

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   coverage start --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"])'
recording
```

## Trigger only the taken branch

Calling `handleBranch(true)` runs the `if` branch; the `else` branch
does not execute and should have count 0.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__oddaCoverageFixture(true))" --browser-id 1 --tab-id 1
"taken-branch"
```

## `coverage snapshot` reads counts mid-recording without stopping

`snapshot` returns the delta since the previous take (or since `start`
if this is the first take). The snapshot must include the fixture
script URL, with at least one block having count > 0 (the taken
branch) and at least one block having count 0 (the not-taken `else`
branch — the negative space).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   coverage snapshot --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> urls = [s["url"] for s in d["scripts"] if s.get("url")]
> fixture_present = any("coverage.html" in u for u in urls)
> counts = [r["count"] for s in d["scripts"] if s.get("url") and "coverage.html" in s["url"]
>            for f in s["functions"] for r in f["ranges"]]
> print(fixture_present, max(counts) > 0, min(counts) == 0)
> '
True True True
```

## `coverage stop` returns the cumulative window and ends recording

`stop` returns the cumulative counts for the whole recording window
(the sum of every take since `start`, including the snapshot above).
The snapshot consumed the first trigger's delta, but `stop`
accumulates it server-side, so after a second trigger the taken
branch has count >= 2; the not-taken `else` branch still has count 0
(it never ran).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__oddaCoverageFixture(true))" --browser-id 1 --tab-id 1
"taken-branch"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   coverage stop --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> counts = [r["count"] for s in d["scripts"] if s.get("url") and "coverage.html" in s["url"]
>            for f in s["functions"] for r in f["ranges"]]
> print(max(counts) >= 2, min(counts) == 0)
> '
True True
```

A second `stop` errors: the recording flag was cleared by the first
`stop`.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   coverage stop --browser-id 1 --tab-id 1
[1]
{"error": "Server error (-32602): Tab 1 is not recording coverage."}
```

## Coverage is per-tab: starting on one tab does not affect another

Open a second tab and confirm a fresh recording on it is independent
of the recording on tab 1 (already stopped).

```scrut
$ port=$(cat "$PWD/fixture_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   tabs open --browser-id 1 --url "http://127.0.0.1:$port/coverage.html" \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["tab_id"])'
2
```

Start coverage on tab 2; tab 1 is not recording (already stopped).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   coverage start --browser-id 1 --tab-id 2 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"])'
recording
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__oddaCoverageFixture(true))" --browser-id 1 --tab-id 2
"taken-branch"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   coverage stop --browser-id 1 --tab-id 2 \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> counts = [r["count"] for s in d["scripts"] if s.get("url") and "coverage.html" in s["url"]
>            for f in s["functions"] for r in f["ranges"]]
> print(max(counts) >= 1, min(counts) == 0)
> '
True True
```

## Navigation persists the recording window (ADR-0005)

The recording flag, accumulator, and CDP Profiler domain all survive
main-frame navigation. The canonical workflow is `start` → `navigate`
(to trigger the behavior under investigation) → `snapshot`/`stop` to
read which code paths ran across the load. After navigating, the tab
is still recording and `snapshot` returns counts that include the
freshly-navigated page's script.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   coverage start --browser-id 1 --tab-id 1 > /dev/null
```

Navigate to the fixture page (this loads `coverage.html` and runs its
inline script, which defines `handleBranch`).

```scrut
$ navigate_fixture /coverage.html "typeof window.__oddaCoverageFixture === 'function'"
```

`snapshot` after the navigate must still work (the recording flag
survived) and must include the fixture script URL with at least one
block having count > 0 (the script ran on load). Capture the max
navigate-time count to a file so the `stop` assertion can prove the
accumulator retained it across the rest of the window.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   coverage snapshot --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> urls = [s["url"] for s in d["scripts"] if s.get("url")]
> fixture_present = any("coverage.html" in u for u in urls)
> counts = [r["count"] for s in d["scripts"] if s.get("url") and "coverage.html" in s["url"]
>            for f in s["functions"] for r in f["ranges"]]
> print(fixture_present, max(counts) > 0, max(counts))
> ' > "$PWD/nav_snapshot.txt"
```

```scrut
$ cat "$PWD/nav_snapshot.txt"
True True 1
```

Triggering the handler and then `stop`ping must return cumulative
counts that include the navigate-time run — the accumulator did not
reset on navigate. The `stop` max count for the fixture must be at
least the navigate-time snapshot max (captured above), proving the
navigate-time blocks are still in the accumulator at `stop`.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__oddaCoverageFixture(true))" --browser-id 1 --tab-id 1
"taken-branch"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   coverage stop --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> counts = [r["count"] for s in d["scripts"] if s.get("url") and "coverage.html" in s["url"]
>            for f in s["functions"] for r in f["ranges"]]
> nav_max = int(open(sys.argv[1]).read().split()[-1])
> print(max(counts) >= nav_max, min(counts) == 0)
> ' "$PWD/nav_snapshot.txt"
True True
```

## Errors on a missing tab

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   coverage start --browser-id 1 --tab-id 9999
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   coverage snapshot --browser-id 1 --tab-id 9999
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   coverage stop --browser-id 1 --tab-id 9999
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

## Errors on a missing browser

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   coverage start --browser-id 9999 --tab-id 1
[1]
{"error": "Server error (-32602): Browser 9999 not found."}
```

## Teardown: stop the fixture server

```scrut
$ stop_fixture_server
```