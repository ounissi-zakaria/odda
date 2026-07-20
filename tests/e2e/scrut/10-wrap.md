---
prepend:
  - _lib/boot.md
append:
  - _lib/teardown.md
---

# Wrap: install, list, remove, dump, and clear function and property wraps

`odda wrap` installs a transparent wrapper at a named function or
property accessor. The wrapper is generated as a named userscript and
installed via the existing userscript mechanism, so it runs at
`document_start` on every navigation and reaches all frames
(including cross-origin iframes, inherited from the extension's
`all_frames: true`). Each call or access is recorded with its
receiver, arguments, return value, and call stack. Functions in
args/ret/this are serialized as opaque refs `{type: "function",
name}`; large or cyclic values are truncated. Per ADR-0003, wraps are
leaf-only: they record the call they were placed on, not callbacks
passed as arguments. Per ADR-0004, records wipe on navigation;
installations persist (the userscript re-runs on every load).

## Helper: spin up a tiny local HTTP server

```scrut {detached: true, detached_kill_signal: term}
$ ( mkdir -p "$PWD/site" && \
>   cp "$TESTDIR/fixtures/wrap.html" "$PWD/site/wrap.html" && \
>   cp "$TESTDIR/fixtures/iframe-inner.html" "$PWD/site/iframe-inner.html" && \
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

Navigate to the fixture page so the wrap userscript has a target.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://127.0.0.1:8766/wrap.html --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: http://127.0.0.1:8766/wrap.html
```

```scrut
$ sleep 1
```

## `wrap calls add` installs a function wrap

Install a wrap on `JSON.parse`. The wrap takes effect on the next
navigation (the userscript runs at `document_start`), so re-navigate
after installing.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap calls add --browser-id 1 --tab-id 1 --expr JSON.parse --name jp \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["name"], d["type"], d["expr"])'
jp call JSON.parse
```

## `wrap list` returns the installed wrap

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap list --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print([(w["name"], w["type"], w["expr"]) for w in d])'
[('jp', 'call', 'JSON.parse')]
```

Re-navigate so the wrap userscript runs at `document_start`.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://127.0.0.1:8766/wrap.html --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: http://127.0.0.1:8766/wrap.html
```

```scrut
$ sleep 1
```

## Triggering the wrapped function records args, ret, and stack

Call `JSON.parse` via the fixture's `__oddaWrapParseAndReturn`
helper, then dump the records.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__oddaWrapParseAndReturn('{\"a\": 1}').a)" --browser-id 1 --tab-id 1
"1"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap dump --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> print(len(d) >= 1)
> r = d[0]
> print(r["wrap"], r["type"])
> print(r["args"] == ["""{\"a\": 1}"""])
> print(r["ret"] == {"a": 1})
> print(isinstance(r["stack"], list) and len(r["stack"]) >= 1)
> print(all(set(f.keys()) == {"fn", "url", "line", "col"} for f in r["stack"]))
> '
True
jp call
True
True
True
True
```

## Opaque function refs appear in args

Install a wrap on `EventTarget.prototype.addEventListener`, trigger
it with a function argument, and confirm the function arg is
serialized as `{type: "function", name}` (not the function body).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap calls add --browser-id 1 --tab-id 1 \
>     --expr EventTarget.prototype.addEventListener --name ael \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["name"], d["type"])'
ael call
```

Re-navigate so the new wrap takes effect.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://127.0.0.1:8766/wrap.html --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: http://127.0.0.1:8766/wrap.html
```

```scrut
$ sleep 1
```

Trigger `addEventListener` with a named callback via the fixture.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__oddaWrapFixture(document.body, 'click', function myHandler() {}))" \
>     --browser-id 1 --tab-id 1
"registered"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap dump --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> ael = [r for r in d if r["wrap"] == "ael"]
> print(len(ael) >= 1)
> r = ael[0]
> print(r["args"][0])
> print(json.dumps(r["args"][1]))
> print(json.dumps(r["ret"]))
> '
True
click
{"type": "function", "name": "myHandler"}
null
```

Per ADR-0003 the wrap is leaf-only: the callback `myHandler` is
recorded as an opaque ref in the args, but it is not itself wrapped
(no records will appear from invoking it later).

## `wrap access add` records a property setter

Install an access wrap on `HTMLElement.prototype.innerHTML`, then
trigger it by setting `innerHTML` on an element.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap access add --browser-id 1 --tab-id 1 \
>     --expr HTMLElement.prototype.innerHTML --name ih \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["name"], d["type"], d["expr"])'
ih access HTMLElement.prototype.innerHTML
```

Re-navigate so the wrap takes effect.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://127.0.0.1:8766/wrap.html --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: http://127.0.0.1:8766/wrap.html
```

```scrut
$ sleep 1
```

Trigger the setter via the fixture's `__oddaWrapSetSink` helper.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__oddaWrapSetSink('<b>hi</b>'))" --browser-id 1 --tab-id 1
"<b>hi</b>"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap dump --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> ih = [r for r in d if r["wrap"] == "ih" and r["type"] == "access"]
> print(len(ih) >= 1)
> r = ih[0]
> # A set records args[0] as the value written; ret is null.
> print(r["args"] == ["<b>hi</b>"])
> print(json.dumps(r["ret"]))
> print("this" in r and isinstance(r["stack"], list))
> '
True
True
null
True
```

## `wrap clear` zeros records without navigating

Clear the records and confirm `dump` returns an empty array.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap clear --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["count"] >= 0)'
cleared True
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap dump --browser-id 1 --tab-id 1
[]
```

## Records are wiped on navigation

After clearing, trigger the wrap again to get a record, then navigate
and confirm the records are gone (per ADR-0004).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__oddaWrapParseAndReturn('{\"x\": 1}').x)" --browser-id 1 --tab-id 1
"1"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap dump --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(any(r["wrap"] == "jp" for r in d))'
True
```

Navigate (which wipes the records) and confirm `dump` is empty.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://127.0.0.1:8766/wrap.html --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: http://127.0.0.1:8766/wrap.html
```

```scrut
$ sleep 1
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap dump --browser-id 1 --tab-id 1
[]
```

The wrap installation persists across navigation: triggering
`JSON.parse` again records again, without re-installing.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__oddaWrapParseAndReturn('{\"y\": 2}').y)" --browser-id 1 --tab-id 1
"2"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap dump --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(any(r["wrap"] == "jp" for r in d))'
True
```

## Wraps reach all frames (inherited from `all_frames: true`)

The fixture page has a same-origin `srcdoc` iframe. The userscript
extension's `all_frames: true` means the wrap runs in the iframe too,
and same-origin iframes aggregate records to the top frame's
`__oddaWrap` (so `wrap dump` on the main tab sees them).

Clear records, then call `JSON.parse` inside the iframe.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap clear --browser-id 1 --tab-id 1 > /dev/null
```

```scrut
$ printf '%s\n' \
>   "var f = document.getElementById('xframe');" \
>   "var inner = f.contentWindow;" \
>   "inner.JSON.parse('{\"inf\": 1}');" \
>   "'ok'" > "$PWD/iframe-eval.js"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --file "$PWD/iframe-eval.js" --browser-id 1 --tab-id 1
"ok"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap dump --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(any(r["wrap"] == "jp" for r in d))'
True
```

The wrap reached the iframe (inherited from the userscript
extension's `all_frames: true`) and the record aggregated to the top
frame.

## Large values are truncated

The serializer truncates arrays longer than 100 elements. Install a
wrap on a function that returns a large array, trigger it, and
confirm the return value is truncated.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap calls add --browser-id 1 --tab-id 1 --expr Array.of --name big \
>   > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://127.0.0.1:8766/wrap.html --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: http://127.0.0.1:8766/wrap.html
```

```scrut
$ sleep 1
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "Array.of.apply(null, new Array(150).fill(0).map(function(_, i) { return i; })); 'ok'" \
>     --browser-id 1 --tab-id 1
"ok"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap dump --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> big = [r for r in d if r["wrap"] == "big"]
> print(len(big) >= 1)
> r = big[0]
> print(isinstance(r["ret"], dict) and r["ret"].get("type") == "array" and r["ret"].get("truncated") == True)
> print(r["ret"]["length"] == 150)
> '
True
True
True
```

## `wrap remove` stops recording on future navigations

Remove the `jp` wrap and re-navigate. The wrap's userscript is gone,
so future `JSON.parse` calls do not record.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap remove --browser-id 1 --tab-id 1 jp \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["name"], d["removed"])'
jp True
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap list --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sorted(w["name"] for w in d))'
['ael', 'big', 'ih']
```

Re-navigate so the removed wrap's userscript no longer runs.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://127.0.0.1:8766/wrap.html --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: http://127.0.0.1:8766/wrap.html
```

```scrut
$ sleep 1
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__oddaWrapParseAndReturn('{\"z\": 3}').z)" --browser-id 1 --tab-id 1
"3"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap dump --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(any(r["wrap"] == "jp" for r in d))'
False
```

The other wraps (`ael`, `ih`) are still installed and still record on
this navigation.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "String(window.__oddaWrapSetSink('<i>bye</i>'))" --browser-id 1 --tab-id 1
"<i>bye</i>"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap dump --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(any(r["wrap"] == "ih" for r in d))'
True
```

## Errors on a missing tab

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap calls add --browser-id 1 --tab-id 9999 --expr JSON.parse --name x
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap access add --browser-id 1 --tab-id 9999 --expr document.cookie --name x
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap list --browser-id 1 --tab-id 9999
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap remove --browser-id 1 --tab-id 9999 jp2
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap dump --browser-id 1 --tab-id 9999
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap clear --browser-id 1 --tab-id 9999
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

## Errors on a missing browser

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap list --browser-id 9999 --tab-id 1
[1]
{"error": "Server error (-32602): Browser 9999 not found."}
```

## `wrap remove` on a non-existent wrap errors

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap remove --browser-id 1 --tab-id 1 no-such-wrap
[1]
{"error": "Server error (-32602): Userscript '__odda-wrap__no-such-wrap' not found"}
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