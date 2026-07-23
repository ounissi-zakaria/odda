---
prepend:
  - _lib/boot.md
  - _lib/fixture-server.md
  - _lib/browser-fixture.md
append:
  - _lib/teardown.md
---

# Page interaction: snapshot, click, fill, hover, upload

`odda page` drives the browser to trigger behavior — clicking,
filling, hovering, uploading, and snapshotting the page to find
targets. The agent snapshots to discover element refs (`[ref=eN]`,
or `[ref=f<frameSeq>eN]` inside an iframe), then passes `--ref eN`
to `click`, `fill`, `hover`, and `upload` to identify the target.
Refs are valid as long as their element remains in the DOM; if the
element is removed (SPA content swap, navigation), the action errors
cleanly. Re-snapshot to discover refs for new elements; existing refs
continue to work without re-snapshotting.

All commands are tab-scoped (`--browser-id` + `--tab-id`). Actions
accept `--timeout` (default 5s) for ref resolution and the action
itself; a stale ref errors cleanly rather than hanging.

## Set up the fixture server and browser

The fixture page has a clickable button, a text input, a hover
target, a file input, and an iframe with its own clickable button.
Each interactive element has an observable effect the tests read
back via `eval` to confirm the action landed.

```scrut
$ setup_fixture_site page-interaction.html page-interaction-inner.html
```

```scrut {detached: true, detached_kill_signal: term}
$ port=$(cat "$PWD/fixture_port"); ( python3 -m http.server "$port" --bind 127.0.0.1 --directory "$PWD/site" >"$PWD/http.log" 2>&1 < /dev/null & )
```

```scrut
$ wait_for_fixture_server
```

```scrut
$ open_browser_fixture /page-interaction.html "window.__oddaPageIframeLoaded !== undefined || true"
```

Wait for the iframe to load so snapshot can reach into it.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wait-for --expression "document.getElementById('inner-frame').contentWindow.__oddaPageIframeLoaded === true" \
>   --browser-id 1 --tab-id 1 --timeout 10 > /dev/null
```

## `page snapshot` returns the accessibility tree with refs

The snapshot is text: a YAML-ish serialization of the page's
accessibility tree. Each element is tagged with `[ref=eN]` (or
`[ref=f<frameSeq>eN]` inside an iframe). The agent greps for the
element it wants.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   page snapshot --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys, re
> d = json.load(sys.stdin)
> print(isinstance(d, str))
> print("[ref=e" in d)
> print("Click me" in d)
> print("textbox" in d.lower() or "text" in d.lower())
> print(bool(re.search(r"\[ref=f\d+e\d+\]", d)))
> print("Iframe button" in d)
> '
True
True
True
True
True
True
```

## `page click` clicks the element identified by `--ref`

Snapshot, find the "Click me" button's ref, click it, and verify the
page's click handler fired (the result div changes from "not clicked"
to "clicked").

```scrut
$ REF=$(odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   page snapshot --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys, re
> d = json.load(sys.stdin)
> line = next(l for l in d.splitlines() if "Click me" in l)
> m = re.search(r"\[ref=(e\d+)\]", line)
> print(m.group(1))
> ')
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   page click --browser-id 1 --tab-id 1 --ref "$REF" \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["ref"] == "'"$REF"'")'
clicked True
```

The click handler set the result div to "clicked".

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "document.getElementById('click-result').textContent" --browser-id 1 --tab-id 1
"clicked"
```

## `page click` reaches into iframes

The snapshot includes iframe content with refs of the form
`f<frameSeq>e<elemNum>`. Clicking an iframe ref fires the handler
inside the iframe.

```scrut
$ IREF=$(odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   page snapshot --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys, re
> d = json.load(sys.stdin)
> line = next(l for l in d.splitlines() if "Iframe button" in l)
> m = re.search(r"\[ref=(f\d+e\d+)\]", line)
> print(m.group(1))
> ')
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   page click --browser-id 1 --tab-id 1 --ref "$IREF" \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["ref"] == "'"$IREF"'")'
clicked True
```

The iframe's click handler set its result div.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "document.getElementById('inner-frame').contentWindow.document.getElementById('iframe-result').textContent" --browser-id 1 --tab-id 1
"clicked in iframe"
```

## `page fill` fills the element identified by `--ref`

Snapshot, find the textbox ref, fill it, and verify the input handler
fired (the result div echoes the value). Fill clears first, so the
second fill replaces, not appends.

```scrut
$ TREF=$(odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   page snapshot --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys, re
> d = json.load(sys.stdin)
> line = next(l for l in d.splitlines() if "Type here" in l)
> m = re.search(r"\[ref=(e\d+)\]", line)
> print(m.group(1))
> ')
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   page fill --browser-id 1 --tab-id 1 --ref "$TREF" --value "hello" \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["ref"] == "'"$TREF"'")'
filled True
```

The input handler set the result div to the typed value.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "document.getElementById('text-result').textContent" --browser-id 1 --tab-id 1
"hello"
```

Fill clears first: filling "world" replaces "hello", not appends.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   page fill --browser-id 1 --tab-id 1 --ref "$TREF" --value "world" > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "document.getElementById('text-input').value" --browser-id 1 --tab-id 1
"world"
```

## `page hover` hovers the element identified by `--ref`

Snapshot, find the "Hover me" ref, hover it, and verify the hover
handler fired (the result div changes from "not hovered" to "hovered").

```scrut
$ HREF=$(odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   page snapshot --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys, re
> d = json.load(sys.stdin)
> line = next(l for l in d.splitlines() if "Hover me" in l)
> m = re.search(r"\[ref=(e\d+)\]", line)
> print(m.group(1))
> ')
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   page hover --browser-id 1 --tab-id 1 --ref "$HREF" \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["ref"] == "'"$HREF"'")'
hovered True
```

The hover handler set the result div.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "document.getElementById('hover-result').textContent" --browser-id 1 --tab-id 1
"hovered"
```

## `page upload` sets files on a file input identified by `--ref`

A `<input type="file">` without an accessible name does not appear in
the a11y tree snapshot (Playwright omits nameless file inputs). The
agent gives it an `aria-label` via `eval`, re-snapshots, then uploads.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "document.getElementById('file-input').setAttribute('aria-label', 'Upload files'); 'ok'" --browser-id 1 --tab-id 1
"ok"
```

Re-snapshot and find the file input's ref.

```scrut
$ UFREF=$(odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   page snapshot --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys, re
> d = json.load(sys.stdin)
> line = next(l for l in d.splitlines() if "Upload files" in l)
> m = re.search(r"\[ref=(e\d+)\]", line)
> print(m.group(1) if m else "")
> ')
```

```scrut
$ echo "$UFREF" | python3 -c 'import sys; s=sys.stdin.read().strip(); print("ref found" if s else "no ref")'
ref found
```

Create a temp file to upload.

```scrut
$ echo "test file content" > "$PWD/upload-test.txt"
```

Upload it. The return includes the ref and the file paths.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   page upload --browser-id 1 --tab-id 1 --ref "$UFREF" --file "$PWD/upload-test.txt" \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> print(d["status"], d["ref"] == "'"$UFREF"'")
> print(len(d["files"]) == 1)
> print("upload-test.txt" in d["files"][0])
> '
uploaded True
True
True
```

The change handler fired and the result div lists the file name.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "document.getElementById('upload-result').textContent" --browser-id 1 --tab-id 1
"upload-test.txt"
```

## Stale ref errors cleanly (no 30s hang)

Navigate away (the old refs' elements are removed), then try to click
an old ref. odda returns a clean error quickly, not a Playwright
30-second timeout. Using `--timeout 2` keeps the test fast.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate --url "about:blank" --browser-id 1 --tab-id 1 > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   page click --browser-id 1 --tab-id 1 --ref "$HREF" --timeout 2 2>&1 \
>   | python3 -c '
> import json, sys
> d = json.load(sys.stdin)
> print("error" in d)
> print("take a new snapshot" in d.get("error", ""))
> '
True
True
```

## Errors on a missing tab (click)

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   page click --browser-id 1 --tab-id 9999 --ref e1
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

## Errors on a missing browser (fill)

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   page fill --browser-id 9999 --tab-id 1 --ref e1 --value "x"
[1]
{"error": "Server error (-32602): Browser 9999 not found."}
```

## Errors on a missing tab (snapshot)

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   page snapshot --browser-id 1 --tab-id 9999
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

## Errors on a missing browser (snapshot)

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   page snapshot --browser-id 9999 --tab-id 1
[1]
{"error": "Server error (-32602): Browser 9999 not found."}
```

## Teardown: stop the fixture server

```scrut
$ stop_fixture_server
```