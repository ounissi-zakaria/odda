---
prepend:
  - _lib/boot.md
  - _lib/browser-fixture.md
append:
  - _lib/teardown.md
---

# Targeting model: errors on bad browser/tab IDs

Every browser/tab command takes an explicit target. Unknown or stale
IDs must error cleanly with a JSON `{"error": ...}` body and a
non-zero exit code, never silently hit a different tab/browser.

## Set up a browser

```scrut
$ open_browser_fixture
```

## `navigate` errors on an unknown browser_id

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://x --browser-id 9999 --tab-id 1
[1]
{"error": "Server error (-32602): Browser 9999 not found."}
```

## `navigate` errors on an unknown tab_id

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://x --browser-id 1 --tab-id 9999
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

## `eval` errors on an unknown tab_id

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "1" --browser-id 1 --tab-id 9999
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

## `screenshot` errors on an unknown browser_id

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   screenshot --browser-id 9999 --tab-id 1
[1]
{"error": "Server error (-32602): Browser 9999 not found."}
```

## `tabs open` errors on an unknown browser_id

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   tabs open --browser-id 9999
[1]
{"error": "Server error (-32602): Browser 9999 not found."}
```

## `tabs close` errors on an unknown tab_id

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   tabs close --browser-id 1 --tab-id 9999
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

## `event-listeners` errors on an unknown browser_id

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   event-listeners --browser-id 9999 --tab-id 1
[1]
{"error": "Server error (-32602): Browser 9999 not found."}
```