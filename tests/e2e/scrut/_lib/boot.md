## Boot the odda server

```scrut
$ nohup odda server --socket "$PWD/odda.sock" --data-dir "$PWD/data" --log "$PWD/server.log" \
>   >"$PWD/server.log" 2>&1 &
```

```scrut
$ for i in $(seq 1 2000); do test -S "$PWD/odda.sock" && exit 0; sleep 0.03; done; exit 1
```