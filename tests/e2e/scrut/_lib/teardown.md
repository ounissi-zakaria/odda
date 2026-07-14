## Teardown: stop the odda server

```scrut
$ pkill -f "odda.*--data-dir $PWD/data" 2>/dev/null || true
```