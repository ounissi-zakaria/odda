# Upstream proxy: runtime mode swap, per-session tool config

odda's proxy can chain all of its server-side traffic through a second
forward proxy (mitmproxy's upstream mode): Chrome keeps connecting to
odda's proxy, and odda forwards everything to the upstream instead of
direct. The surface is three MCP tools — `proxy_upstream_set(url,
auth=None)`, `proxy_upstream_clear()`, `proxy_upstream_get()` — and the
configuration is per-session: nothing is persisted, every session boots
direct.

The mechanism research lives in
`.scratch/upstream-proxy/mitmproxy-upstream-options.md` (installed
mitmproxy 12.2.3, code-anchored). Decisions recorded here are the ones a
future reader will re-litigate.

**The mechanism is a runtime `options.mode` swap, not a second process or
a rebuild.** `ProxyServer.set_upstream` assigns
`options.mode = ["upstream:<url>@<listen_host>:<listen_port>"]` (the
mode-spec `@` suffix pins odda's existing listener address; without it an
upstream mode would default to port 8080). mitmproxy re-parses the spec
synchronously on assignment, rolls back on a bad one, rebuilds the
listener as a loop task (stop-before-start: the port is briefly
unbound), and only connections accepted after the rebind use the new
mode. odda awaits the rebind with a data-less TCP probe before the tool
returns, so callers never race the window. Keep-alives finishing on the
old path are acceptable — each new request re-dials — and the tool
results carry that note verbatim (`_FLIP_NOTE`).

**Config is tools-only and per-session.** Env vars were rejected twice
over: mitmproxy never consults `http_proxy`-style variables for its
server-side connections (no free mechanism there), and an env-seeded
default would fire on every session in every network — a machine-scoped
secret leaking into project workflows. Persistence (`.odda/`, like
proxy-scripts) was rejected because an upstream is a property of the
network moment, not of the project: a silently restored corporate proxy
on a different network produces mystery 502s. The harness that needs
session-start upstreams instructs its agent to call
`proxy_upstream_set` first; agents that need to rotate vantage
mid-investigation just call it again.

**All traffic, no split routing.** mitmproxy has no native per-host
routing table; split routing is achievable per-flow today by a
proxy-script setting `flow.server_conn.via`
(mitmproxy's `change_upstream_proxy.py` example), which needs no odda
code — so odda's native surface stays the honest all-or-nothing switch.

**`request_send` stays direct.** The raw request path opens its own
sockets and deliberately bypasses odda's proxy (wire-faithful contract);
it also bypasses the upstream. That is a documented vantage split, not
an oversight: routing crafted bytes through a CONNECT tunnel would grow
the sender contract for a need (corporate egress) that has a cleaner
fix later (a proxy-dial option in `src/odda/request/`) if it materializes.

**Loopback keeps `--proxy-bypass-list=<-loopback>` unconditionally.**
Forcing loopback through odda's proxy is load-bearing for e2e fixture
capture, and making the flag conditional on upstream state would give
browsers opened before/after a flip different behavior. So under an
upstream, loopback targets fail loudly (the CONNECT dies at the
upstream; `flow.error` records the refusal) instead of silently losing
local capture — loud failures are agent-debuggable, absent flows are
not. The rule lives in the tool descriptions and the
`odda://docs/upstream-proxy` resource: clear the upstream before
driving 127.0.0.1 targets.

**Basic auth only, via `upstream_auth`.** mitmproxy's grammar has no
credentials field in the server spec (userinfo in the URL is a hard
parse error), so odda rejects `user:pass@host` URLs with a pointer to
the `auth` parameter and maps `auth` to the `upstream_auth` option,
which is set *before* the mode swap so no unauthenticated CONNECT
reaches the upstream. NTLM/Bearer remain proxy-script territory (the
`http_connect_upstream` hook); SOCKS has no support to expose.

**Failures are per-flow, never fatal.** An unreachable/refusing upstream
surfaces as odda `502`s (plain http), dead TLS handshakes (tunneled
https), and populated `flow.error` — the embedded master keeps serving,
matching how odda already treats proxy-script failures.
