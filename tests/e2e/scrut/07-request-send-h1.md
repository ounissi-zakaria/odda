---
prepend:
  - _lib/boot.md
  - _lib/fixture-server.md
append:
  - _lib/teardown.md
---

# `odda request new` + `request send` (HTTP/1.1)

Create an empty editable request, fill in a raw HTTP/1.1 request,
and `send` it. The sent request is recorded as a flow with the
scheme + port in `flows.jsonl`.

## `request new` creates an empty `request` and a `meta.json`

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name h1-test --host xs2.top \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["name"], d["scheme"], d["host"])'
h1-test https xs2.top
```

```scrut
$ test -e "$PWD/data/requests/h1-test/request" && stat -c '%s' "$PWD/data/requests/h1-test/request"
0
```

## Write a raw HTTP/1.1 GET request to the `request` file

The `request` file needs CRLF line endings. Use `sed` to convert the
heredoc's LF to CRLF on write.

```scrut
$ sed 's/$/\r/' > "$PWD/data/requests/h1-test/request" <<'REQEOF'
> GET /a?body=h1-send-test&status=200&header=Content-Type:application/json HTTP/1.1
> Host: xs2.top
> Accept: */*
> 
> REQEOF
```

## `request send` records the flow and returns the `flows.jsonl` record

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send h1-test --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status_code"], "id=" + d["id"], d["body_file"])'
200 id=* flows/*/response_body.json (glob)
```

The response body is the body we asked for in the query string.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send h1-test --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["id"])' > "$PWD/flow_id"
```

```scrut
$ flow_id=$(cat "$PWD/flow_id")
```

```scrut
$ cat "$PWD/flow_id"
* (glob)
```


```scrut
$ cat "$PWD/data/flows/$flow_id/response_body.json"
h1-send-test (no-eol)
```

The stored request file uses `HTTP/1.1`.


```scrut
$ grep -F "HTTP/1.1" "$PWD/data/flows/$flow_id/request" >/dev/null && echo "h1 stored" || echo "missing"
h1 stored
```

`flows.jsonl` records the scheme and port.


```scrut
$ grep "\"id\": \"$flow_id\"" "$PWD/data/flows/flows.jsonl" | grep -F '"scheme": "https"' >/dev/null \
>   && grep "\"id\": \"$flow_id\"" "$PWD/data/flows/flows.jsonl" | grep -F '"port": 443' >/dev/null \
>   && echo "jsonl has scheme+port" || echo "missing"
jsonl has scheme+port
```
