# Pre-test cleanup: kill any leftover odda servers and helper HTTP servers

Scrut runs each test document with its own working directory, but
detached processes started by previous runs can outlive those
runs and conflict with new ones (e.g. an HTTP helper from a
previous test doc might be answering on port 8766 when the new
test doc's browser tries to navigate to it).

The test cases here are no-op assertions — they always pass —
but their `pkill` calls ensure a clean slate before the test
doc's own cases run.

```scrut
$ pkill -f "odda server --socket /tmp/execution.*--data-dir /tmp/execution.*" 2>/dev/null || true
```

```scrut
$ pkill -f "http.server 8765.*/tmp/execution.*" 2>/dev/null || true
```

```scrut
$ pkill -f "http.server 8766.*/tmp/execution.*" 2>/dev/null || true
```

```scrut
$ pkill -f "127.0.0.1.*8771\|HTTPServer.*8771" 2>/dev/null || true
```

```scrut
$ pkill -f "interactsh-client -n 1" 2>/dev/null || true
```

```scrut
$ sleep 1
```

```scrut
$ pgrep -af "odda server --socket /tmp/execution" >/dev/null && echo "odda still running" || echo "odda clean"
odda clean
```
