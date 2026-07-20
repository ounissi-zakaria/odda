---
prepend:
  - _lib/boot.md
append:
  - _lib/teardown.md
---

# Coverage: start, snapshot, and stop block-level hit counts

`odda coverage` exposes three commands — `start`, `snapshot`, `stop` —
that record which code blocks execute during a window of interest.
Coverage is per-tab; starting on one tab does not affect another. The
recording window resets on navigation. Zero-hit blocks are included
(the negative space is as informative as the positive).

## Helper: spin up a tiny local HTTP server

```scrut {detached: true, detached_kill_signal: term}
$ ( mkdir -p "$PWD/site" && \
>   cp "$TESTDIR/fixtures/coverage.html" "$PWD/site/coverage.html" && \
>   python3 -m http.server 8766 --bind 127.0.0.1 --directory "$PWD/site" \
>     >"$PWD/http.log" 2>&1 & )
```

```scrut
$ for i in $(seq 1 30); do curl -s -o /dev/null http://127.0.0.1:8766/ && exit 0; sleep 0.5; done; exit 1
```

## Set up a browser

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" browser open --headless \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["browser_id"], d["tab_id"])'
1 1
```

## Navigate to the fixture page

The fixture defines `window.__oddaCoverageFixture(flag)` with an
`if (flag) { ... } else { ... }` branch.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://127.0.0.1:8766/coverage.html --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: http://127.0.0.1:8766/coverage.html
```

```scrut
$ sleep 1
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
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   tabs open --browser-id 1 --url http://127.0.0.1:8766/coverage.html \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["tab_id"])'
2
```

```scrut
$ sleep 1
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

## Navigation resets the recording window

After navigation, the recording flag must be cleared — `snapshot` on a
tab that navigated away must error as "not recording".

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   coverage start --browser-id 1 --tab-id 1 > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://127.0.0.1:8766/coverage.html --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: http://127.0.0.1:8766/coverage.html
```

```scrut
$ sleep 1
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   coverage snapshot --browser-id 1 --tab-id 1
[1]
{"error": "Server error (-32602): Tab 1 is not recording coverage."}
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

## Teardown: stop the local HTTP server

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