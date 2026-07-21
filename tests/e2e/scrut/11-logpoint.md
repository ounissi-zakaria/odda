---
prepend:
  - _lib/boot.md
append:
  - _lib/teardown.md
---

# Logpoint: plant, list, remove, dump, and clear non-pausing source-location observations

`odda logpoint` plants a non-pausing observation at a source location
(script URL, line, column) identified by the agent. odda plants a CDP
`Debugger.setBreakpointByUrl` whose condition evaluates the agent's
`--expr` in the paused-then-immediately-resumed frame's scope and
records the result. The page never pauses. Logpoints persist until
explicitly removed (not fire-once); records wipe on navigation; the
CDP breakpoint re-binds to the re-loaded script. Logpoints do not
survive tab close (per-tab-session). Use it when you know the line.

The agent supplies `--url`, `--line` (0-based), `--col` (0-based), and
`--expr`. Minified code packs many statements per line, so the column
is required to hit the right statement.

## Helper: spin up a tiny local HTTP server

```scrut {detached: true, detached_kill_signal: term}
$ ( mkdir -p "$PWD/site" && \
>   cp "$TESTDIR/fixtures/logpoint.html" "$PWD/site/logpoint.html" && \
>   cp "$TESTDIR/fixtures/logpoint.js" "$PWD/site/logpoint.js" && \
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

The fixture defines `window.__oddaLogpointFixture(name)` in an external
`logpoint.js` so line/column numbers are 0-based offsets in the file.
The function greet starts on line 0, the greeting assignment is on
line 1, the return is on line 2, the closing brace is on line 3, and
the fixture export is on line 4.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://127.0.0.1:8766/logpoint.html --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: http://127.0.0.1:8766/logpoint.html
```

```scrut
$ sleep 1
```

## `logpoint add` plants a non-pausing observation that records a local

Plant a logpoint at line 2, col 0 (the `return greeting;` statement),
with expression `greeting`. At that point `greeting` has been assigned
on line 1, so the logpoint records its value.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint add --browser-id 1 --tab-id 1 \
>     --url http://127.0.0.1:8766/logpoint.js --line 2 --col 0 \
>     --expr "greeting" \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> print(d["status"], "id" in d, "warning" not in d)
> '
planted True True
```

Now trigger the function. The fixture helper calls `greet('world')`,
which hits the logpoint line. The page does not pause.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__oddaLogpointFixture('world'))" --browser-id 1 --tab-id 1
"hello world"
```

## `logpoint dump` returns the recorded value

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint dump --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> print(len(d) >= 1)
> r = d[0]
> print("logpoint" in r and "url" in r and "line" in r and "col" in r)
> print(r["url"])
> print(r["line"], r["col"])
> print(r["value"])
> print(json.dumps(r["error"]))
> '
True
True
http://127.0.0.1:8766/logpoint.js
2 0
hello world
null
```

## `logpoint list` returns the planted logpoint

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint list --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> print(len(d) == 1)
> lp = d[0]
> print(lp["url"], lp["line"], lp["col"], lp["expr"])
> '
True
http://127.0.0.1:8766/logpoint.js 2 0 greeting
```

## A wrong local name produces an error record

Plant a second logpoint at line 1 (the greeting assignment) whose
expression references a local that does not exist. The expression
throws a ReferenceError; the record carries `{error:
"ReferenceError: ..."}` rather than silently recording nothing.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint add --browser-id 1 --tab-id 1 \
>     --url http://127.0.0.1:8766/logpoint.js --line 1 --col 0 \
>     --expr "noSuchLocal" \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], "warning" not in d)'
planted True
```

Clear records so we only see the new logpoint's output, then trigger.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint clear --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["count"] >= 0)'
cleared True
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__oddaLogpointFixture('world'))" --browser-id 1 --tab-id 1
"hello world"
```

The dump now includes a record whose `error` is a ReferenceError
string and whose `value` is null.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint dump --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> errs = [r for r in d if r.get("error")]
> print(len(errs) >= 1)
> r = errs[0]
> print(json.dumps(r["value"]))
> print("ReferenceError" in r["error"])
> '
True
null
True
```

## Logpoints persist until explicitly removed (not fire-once)

Trigger the fixture again; the first logpoint (`greeting`) records a
second hit. Records accumulate across triggers within one page load.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint clear --browser-id 1 --tab-id 1 > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__oddaLogpointFixture('first'))" --browser-id 1 --tab-id 1
"hello first"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__oddaLogpointFixture('second'))" --browser-id 1 --tab-id 1
"hello second"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint dump --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> greeting = [r for r in d if r.get("value") in ("hello first", "hello second")]
> print(len(greeting) == 2)
> '
True
```

## Records are wiped on navigation; installations persist

Per ADR-0004, records wipe on navigation but the CDP breakpoint
re-binds to the re-loaded script. After re-navigating and triggering,
the logpoint records again without re-planting.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://127.0.0.1:8766/logpoint.html --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: http://127.0.0.1:8766/logpoint.html
```

```scrut
$ sleep 1
```

After navigation, `dump` is empty (records wiped).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint dump --browser-id 1 --tab-id 1
[]
```

The installation persisted: triggering again records again.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__oddaLogpointFixture('after-nav'))" --browser-id 1 --tab-id 1
"hello after-nav"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint dump --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> print(any(r.get("value") == "hello after-nav" for r in d))
> '
True
```

The logpoint registry still lists both logpoints after navigation.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint list --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> print(sorted(lp["expr"] for lp in d))
> '
['greeting', 'noSuchLocal']
```

## `logpoint remove` stops recording on future hits

Remove the `noSuchLocal` logpoint. We need its id; list and find it.

```scrut
$ LPID=$(odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint list --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> print(next(lp["id"] for lp in d if lp["expr"] == "noSuchLocal"))
> ')
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint remove --browser-id 1 --tab-id 1 --id "$LPID" \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["id"] == "'"$LPID"'")'
removed True
```

After removal, only the `greeting` logpoint remains.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint list --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> print(sorted(lp["expr"] for lp in d))
> '
['greeting']
```

Clear records, re-navigate so the removed breakpoint is unbound, then
trigger. Only the `greeting` logpoint records.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint clear --browser-id 1 --tab-id 1 > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://127.0.0.1:8766/logpoint.html --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: http://127.0.0.1:8766/logpoint.html
```

```scrut
$ sleep 1
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__oddaLogpointFixture('after-remove'))" --browser-id 1 --tab-id 1
"hello after-remove"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint dump --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> print(any(r.get("value") == "hello after-remove" for r in d))
> print(any(r.get("error") for r in d))
> '
True
False
```

## Stale-URL warning at install

Plant a logpoint at a URL that no loaded script matches. The command
succeeds but the output includes a `warning` field. Triggering the
fixture produces no records from this logpoint (the breakpoint never
binds).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint add --browser-id 1 --tab-id 1 \
>     --url http://127.0.0.1:8766/does-not-exist.js --line 0 --col 0 \
>     --expr "x" \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> print(d["status"], "warning" in d, isinstance(d.get("warning"), str))
> '
planted True True
```

## Errors on a missing tab

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint add --browser-id 1 --tab-id 9999 \
>     --url http://x --line 0 --col 0 --expr x
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint list --browser-id 1 --tab-id 9999
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint dump --browser-id 1 --tab-id 9999
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint clear --browser-id 1 --tab-id 9999
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint remove --browser-id 1 --tab-id 9999 --id lp-nope
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

## Errors on a missing browser

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint list --browser-id 9999 --tab-id 1
[1]
{"error": "Server error (-32602): Browser 9999 not found."}
```

## `logpoint remove` on an unknown id errors

Removing a logpoint id that is not planted on the tab returns a JSON
error with a non-zero exit code.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint remove --browser-id 1 --tab-id 1 --id lp-nope
[1]
{"error": "Server error (-32602): Logpoint lp-nope not found in tab 1."}
```

## `logpoint add` at an already-planted location errors

Planting a second logpoint at the same `(url, line, col)` as an
existing one errors cleanly (CDP allows only one logpoint per
location).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint add --browser-id 1 --tab-id 1 \
>     --url http://127.0.0.1:8766/logpoint.js --line 2 --col 0 \
>     --expr "greeting"
[1]
{"error": "Server error (-32602): A logpoint already exists at http://127.0.0.1:8766/logpoint.js:2:0 (id=lp-1); remove it first."}
```

## Logpoints do not survive tab close (per-tab-session)

Per ADR-0004, logpoint installations are per-tab-session. Close the
tab, open a new one, navigate, and confirm `logpoint list` is empty
(the new tab has no logpoints).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   tabs close --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
closed
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   tabs open --browser-id 1 --url http://127.0.0.1:8766/logpoint.html \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["tab_id"])'
2
```

```scrut
$ sleep 1
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   logpoint list --browser-id 1 --tab-id 2
[]
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