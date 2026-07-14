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

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" version
{"version": "*"} (glob)
```


## `odda status` reports the running server

`status` returns a JSON object with `socket`, `data_dir`, `parent_pid`,
`proxy_url`, and `browser_count` fields. The exact key order is not
part of the contract.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" status \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sorted(d))'
['browser_count', 'data_dir', 'parent_pid', 'proxy_url', 'socket']
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" status \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["browser_count"])'
0
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" status \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["proxy_url"])'
http://127.0.0.1:* (glob)
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" status \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["data_dir"])'
*/data (glob)
```

## `odda proxy-url` returns the proxy URL as plain text

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" proxy-url
http://127.0.0.1:* (glob)
```

## `odda logs` returns the tail of the server log

`logs --n 5` returns `{"lines": [...]}`. On a fresh server the log has
just been seeded with the start-up entries.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" logs --n 5 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print("lines" in d, len(d["lines"]))'
True * (glob)
```

The first line of a fresh server's log mentions "Starting odda server".

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" logs --n 5 \
>   | python3 -c 'import json,sys; print("Starting odda server" in json.load(sys.stdin)["lines"][0])'
True
```

## `flows.jsonl` is absent before any capture

The proxy hasn't been used yet, so the flows index file should not exist.

```scrut
$ test ! -e "$PWD/data/flows/flows.jsonl" && echo "absent"
absent
```
