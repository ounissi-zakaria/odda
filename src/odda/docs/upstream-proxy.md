# Upstream proxy

The `proxy_upstream_*` tools chain odda's proxy through a second forward proxy (mitmproxy's upstream mode). Chrome keeps connecting to odda's proxy; every request odda forwards goes to the upstream instead of direct. Use it to change the session's network vantage: a corporate egress proxy, a residential/datacenter proxy for geo-vantage, or a chained MITM proxy.

## Tools

- `proxy_upstream_set(url, auth=None)` — chain through the upstream; returns the new state.
- `proxy_upstream_clear()` — back to direct egress.
- `proxy_upstream_get()` — current state: `upstream` (URL or `null` when direct) and `auth_set`.

## Constraints

- **http/https upstreams only.** `socks5://` and friends are rejected — mitmproxy's upstream mode has no SOCKS support.
- **Basic auth via `auth`.** Pass `'username:password'`; the credentials are sent as `Proxy-Authorization` on the CONNECT/request to the upstream. Never embed `user:pass@` in the URL — it is rejected. Non-Basic schemes (NTLM, Bearer) need a proxy-script using the `http_connect_upstream` hook (see mitmproxy's `ntlm_upstream_proxy.py` example).
- **All traffic, one scope.** The upstream applies to every flow through the proxy — there is no per-host split. Per-flow routing (some hosts via the upstream, others direct) is possible with a proxy-script that sets `flow.server_conn.via` per request (see mitmproxy's `change_upstream_proxy.py` example).
- **New connections only.** After `proxy_upstream_set`/`clear`, existing keep-alive connections finish on the previous path; the next request each client makes uses the new one.
- **Session-scoped.** The upstream is per-MCP-session state; it is never persisted and every session starts direct.

- **Crafted requests stay direct.** The `request_send` family bypasses odda's proxy entirely, so it also bypasses the upstream: while an upstream is set, browser flows exit via the upstream but crafted requests still exit from this machine's own IP.

## Loopback targets

Do not chain a (remote) upstream while driving loopback targets: `127.0.0.1`/`localhost` requests would be CONNECTed through the upstream, which cannot reach the agent's machine — the page fails (and the flow records the upstream's refusal in `flow.error`). Call `proxy_upstream_clear` first when testing local fixture servers.

## Failure model

The upstream being unreachable, refusing (407), or speaking garbage are **per-flow** errors, never a crash: plain-HTTP flows surface odda's `502 Bad Gateway` with the refusal text; tunneled (https) flows surface a dead TLS handshake after CONNECT. Either way the flow lands in `.odda/flows/` with `flow.error`/`server_conn.error` populated — grep `flows.jsonl` to diagnose. An invalid URL fails at `proxy_upstream_set` time with a tool error and changes nothing.

## Observation

Proxy-scripts can see the chain per flow: `flow.server_conn.via` is the upstream (`("http"|"https", (host, port))`) when chained and `None` when direct; `flow.client_conn.proxy_mode.full_spec` names the serving mode.
