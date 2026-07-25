---
prepend:
  - _lib/boot.md
append:
  - _lib/teardown.md
---

# `odda` server: core commands

Exercises the read-only server commands that don't need a browser: `version`,
`status`, `proxy-url`, and `logs`. Also confirms the proxy data directory
starts empty (no flows captured yet).

## `odda version` returns the package version

Text output is `odda <version>`.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" version
odda * (glob)
```


## `odda status` reports the running server

`status` prints `key: value` lines for `socket`, `data_dir`,
`parent_pid`, `proxy_url`, and `browser_count`.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" status
socket: * (glob)
data_dir: */data (glob)
parent_pid: * (glob)
proxy_url: http://127.0.0.1:* (glob)
browser_count: 0
```

## `odda proxy-url` returns the proxy URL as plain text

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" proxy-url
http://127.0.0.1:* (glob)
```

## `odda logs` prints the tail of the server log

`logs --n 5` prints the last 5 log lines as raw text (no JSON
wrapper). On a fresh server the log has just been seeded with the
start-up entries.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" logs --n 5
* (glob+)
```

The first line of a fresh server's log mentions "Starting odda server".

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" logs --n 5 | head -1 | grep -q "Starting odda server" && echo True
True
```

## `flows.jsonl` is absent before any capture

The proxy hasn't been used yet, so the flows index file should not exist.

```scrut
$ test ! -e "$PWD/data/flows/flows.jsonl" && echo "absent"
absent
```