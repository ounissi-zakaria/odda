# Headed browsers on odda-owned virtual displays

`browser_open` has no `headless` param anymore: every browser is headed
Chrome on a virtual display odda spawns per browser — TigerVNC's `Xvnc`
(X server and VNC server in one process) plus a websockify serving the
vendored noVNC client. A human watches and drives a browser through
`vnc://<host>:<port>` (native client) or `http://<host>:<port>/vnc.html`
(browser tab), reported by `browser_open` and `browser_list`. Supersedes
ADR-0016; retires ADR-0039's headless half (every launch follows the
window — no viewport override — and the window is pinned to the display
geometry for determinism).

Why: an agent that hits a human-only wall (captcha, login, task) no
longer closes and reopens its browser in a visible mode, losing page
state — the human connects to the already-running browser; headless
Chrome's extra fingerprint surface is gone, along with the UA-spoof
helper odda maintained to compensate for it (deleted with the mode);
and no window ever appears on the user's desktop, so nothing steals
focus. One display per browser makes identification structural — the
reported endpoint *is* the browser, so no in-page marker is needed.

## Considered Options

- Keep headless as opt-in or an env escape hatch — rejected: keeps the
  dual code path and the fingerprint surface the change exists to kill;
  containers and servers are covered by virtual displays.
- Headed on the user's real display — rejected: focus stealing.
- One shared display per agent session — rejected: reintroduces the
  "which window is which browser" problem the per-browser display
  solves structurally.
- Xvfb + x11vnc per browser — rejected: two processes and teardown
  paths where Xvnc is one.
- In-page identification (e.g. a `document.title` prefix) — rejected:
  page-observable, a fingerprint in itself.
- Bundling a native VNC viewer in odda — rejected: a GUI dependency in
  a headless MCP package; noVNC's browser tab serves the same
  zero-install goal.

## Consequences

- `Xvnc` is a system requirement like Chrome itself (`browser_open`
  fails fast naming the package when missing); `websockify` is a pip
  dependency; noVNC's static assets are vendored under
  `src/odda/novnc/`.
- The noVNC endpoint binds the operator-configured address (`odda mcp
  --vnc-bind` or `ODDA_VNC_BIND`, flag wins, default `127.0.0.1`) —
  operator-only by design; no tool parameter can expose a browser. The
  raw RFB port is loopback-only always, so the only network-exposed
  surface is the noVNC HTTP+WebSocket endpoint.
- No authentication on any endpoint: a deliberate trusted-network
  posture. Every device that can reach the bound interface can view
  and drive any open browser, cookies included. Remote access is
  SSH-tunnel-first; exposing an interface is the operator's conscious
  choice.
- Display processes carry Linux `PR_SET_PDEATHSIG`: when the MCP
  process dies (SIGTERM included — Python's default handler exits
  without running the lifespan teardown; SIGKILL allows no teardown at
  all), the kernel SIGTERMs each display child, which also makes Xvnc
  remove its `/tmp/.X<n>-lock`. Every clean path still tears down
  explicitly first, and remains the only teardown on non-Linux. The
  fork→prctl race is guarded in the child; a websockify connection
  child (forked per client, PDEATHSIG not inherited across fork) dies
  when its sockets close. Numbers locked by a dead owner are reclaimed
  on the next open, so a pre-fix leak or an uncleanly killed Xvnc does
  not permanently shrink the display pool. Reclamation is
  probe-then-unlink, not atomic: concurrent odda processes racing for
  one number resolve benignly (the loser's Xvnc fails to claim it and
  the spawn loop retries another number), and a recycled owner pid can
  keep a number blocked — conservative, safe direction.
- The e2e suite needs no display management of its own: browsers under
  test create and destroy their displays; xdist workers are isolated
  per browser by construction.
- Headed Chrome exits when its last tab closes (the last window goes
  and the process with it), so `tabs_close` on a browser's final tab
  closes the whole browser and reports `browser_closed: true` — the
  "zero open tabs" state ADR-0010's tests relied on under headless is
  physically gone. Browser-scope tools (`wrap_*`, `userscript_*`)
  remain tab-independent whenever the browser is open.
