# Proxy-scripts: global scope, exec mitmproxy `-s` scripts

odda lets the user install mitmproxy addons on the fly via `odda proxy-script
install`, mirroring the `userscript install` lifecycle (install/list/remove,
on-disk storage under `.odda/proxy-scripts/<name>/script.py`, re-added on
server boot). A proxy-script is a Python file in mitmproxy `-s` script
format: odda execs the file as a module and hands the module namespace to
`self.m.addons.add()`, exactly as mitmproxy's own `load_script` does. The
file's top-level `request`/`response`/`load`/`running`/... functions are the
hooks, or it exposes an `addons = [...]` list for composition. No odda-
specific wrapper, no restricted DSL — full mitmproxy addon power, including
`loader.add_option(...)` and `ctx.options`.

Two decisions are recorded because each was a real trade-off and a future
reader will wonder why.

**Scope is global, not per-browser.** Userscripts are per-browser
(ADR-0010), so "similar to userscripts" could mean per-browser proxy-scripts
too. That was rejected: the proxy is a single `ProxyServer` shared across all
browsers (`BrowserManager(proxy=self.proxy)`), and mitmproxy does not tag a
flow to the browser that originated it. Per-browser scoping would require
either one proxy instance per browser (a large architectural change) or
flow-to-browser attribution on a shared proxy (no clean primitive). The
cost of global scope is that an agent's proxy-script sees every other
agent's traffic through the same server. We accept that because, unlike
wraps/userscripts (which multiple agents run simultaneously and which
polluted each other's results), proxy-scripts are a power-user feature
expected to be installed by one agent acting deliberately on the shared
proxy; the multi-agent safety that ADR-0010 bought for wraps does not
extend to the proxy layer, which is already shared.

**The file is exec'd, not parsed into a declarative spec.** mitmproxy
addons are Python objects, not text. The alternative — a declarative spec
(match host/path → modify headers / block / record) that odda compiles
into an internal addon — would be safe (no code exec) and storable as
JSON, but it is not "mitmproxy addons": it loses custom request/response
logic, side effects, external calls, and option registration, which is the
whole point of accepting arbitrary Python. We exec the user's file in the
server process, with full file and network access. This is not a new trust
boundary for an opencode agent that already runs shell commands with full
privileges; the risk is documented in SKILL.md and the proxy-scripts doc,
not gated. mitmproxy's own failure isolation (`script_error_handler` for
exec failures, `safecall()` for runtime hook errors) is reused as-is: a
bad proxy-script is skipped at install/boot and logged, never blocking the
server, and runtime errors surface via `odda logs`.

`--name` is odda's key; `--force` gates overwrite of an existing
proxy-script of the same name (same semantics as `request clone`/`new`),
which makes odda remove-then-add and so never hit mitmproxy's duplicate-
`.name` `AddonManagerError`. After `addons.add()`, odda fires
`ConfigureHook` then `RunningHook` itself (the master is already running
when install is called mid-session), mirroring mitmproxy's
`ScriptLoader.configure`. odda's `FlowFileAddon` is added first in
`ProxyServer.__init__`, so it is always ahead of user proxy-scripts in the
chain: captured `.odda/flows/<id>/` files record the original
request/response, and a proxy-script's mutations affect what goes
upstream, not what is captured — the desired split for a capture tool.