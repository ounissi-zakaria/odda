# Lazy data dir — `.odda` is created on first state write, not at server boot

The data directory (`.odda` / `--data-dir`) is created lazily on the first
state-producing write (flow capture, `request clone`, `browser open`
userscripts, etc.), not when the server boots. `flowstore.set_data_dir`
remembers the path but no longer mkdirs it; `FlowRecordWriter` creates
`flows/` on the first `alloc_flow_id`; `_setup_logging` no longer touches
the data dir. The OpenCode plugin no longer mkdirs `.odda` at
`session.created`.

Server logs moved out of the data dir into the session dir
(`$XDG_RUNTIME_DIR/odda-<pid>.log`, next to the socket). `odda server`
gains a `--log` flag (default: stderr — no file, no dir created); the
plugin passes `--log <runtime-path>` and injects `ODDA_LOG`. `odda logs`
resolves via `ODDA_LOG` then `status.log_path`. The data dir now holds
only persisted project state (`flows/`, `requests/`, `browsers/`); the
socket and log are session plumbing.

The driver: opening an opencode session littered the project dir with
`.odda/` (and a `server.log`) even when odda was never invoked, because
the plugin eagerly started the server at `session.created` and the server
boot path created the data dir for logging and flow storage. The fix
preserves the "server is always ready" UX (eager start stays) while
making `.odda` appear only when something is actually written into it.

Rejected alternatives: (i) keep `server.log` in `.odda` with a lazy-mkdir
`FileHandler` — defeated the fix because the server logs "Starting odda
server" at boot, creating `.odda` immediately; (iii) suppress boot logs
to keep `.odda` absent — fragile, loses startup diagnostics. Moving
logs to the session dir (decision ii) was the only option that achieved
the invariant while preserving early-crash breadcrumbs.

This is recorded because the lazy-creation invariant is easy to
re-break: a future contributor who "helpfully" re-adds
`DATA_DIR.mkdir(...)` in `set_data_dir` or `_setup_logging` "to make
sure the dir exists early" re-opens the littering bug. Don't. The
invariant is: `.odda` exists ⟹ something has been written into it. If
you need a path under the data dir, mkdir *your* target (with
`parents=True`), not the data dir itself.
