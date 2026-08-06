# Proxy-scripts

`odda proxy-script` manages Python files in mitmproxy `-s` script format that odda execs and adds to the running proxy's addon chain. Install one to intercept, modify, record, or generate HTTP traffic at the proxy layer — before it reaches the browser or the upstream server. This is the reference for the exec contract, scope, and failure model; see [SKILL.md](SKILL.md) for the command surface and ADR-0018 for the design rationale.

## Commands

- `odda proxy-script install --name <name> --file <path>` — Install a `.py` file as a proxy-script. Overwrites an existing proxy-script of the same name only with `--force`. Alternatively, use `--source "<py>"` for inline source (mutually exclusive with `--file`).
- `odda proxy-script list` — List installed proxy-scripts with their names and sizes.
- `odda proxy-script remove --name <name>` — Remove a proxy-script: deletes its on-disk source and removes the live addon from the proxy chain.

## The exec contract (mitmproxy `-s` script format)

A proxy-script is exactly what you'd pass to `mitmproxy -s addon.py`. odda execs the file as a module and hands the **module namespace itself** to the mitmproxy addon manager. Hook methods are top-level functions in the file:

```python
def request(flow):
    flow.request.headers["x-custom"] = "injected"

def response(flow):
    if flow.response.status_code == 200:
        flow.response.headers["x-seen-by"] = "odda"
```

For composition, the file can expose an `addons = [...]` list of class instances instead — mitmproxy's addon manager recurses into it. Both forms work identically to `mitmproxy -s`.

The standard mitmproxy hooks apply: `request`, `response`, `error`, `load`, `running`, `configure`, `done`, `client_connected`, `server_connected`, etc. See the [mitmproxy addons documentation](https://docs.mitmproxy.org/stable/api/events.html) for the full event list. `from mitmproxy import ctx` is available; an addon can register options via `load(loader)` → `loader.add_option(...)` and read them via `ctx.options`.

## Storage and scope

- **Storage.** Source is persisted under `.odda/proxy-scripts/<name>/script.py` (mirrors userscripts' `.odda/browsers/<id>/userscripts/<name>/script.js`). Re-added on server boot: the boot scan re-execs every persisted proxy-script, so an agent's proxy-script survives an odda server restart (new OpenCode session, machine reboot).
- **Scope is global**, not per-browser. The proxy is one shared instance across all browsers, so a proxy-script sees **every** flow from every browser through the odda server. This differs from userscripts (per-browser, ADR-0010): per-browser proxy scoping would require either one proxy instance per browser or flow-to-browser attribution, neither of which the shared proxy supports. The cost is that an agent's proxy-script sees other agents' traffic through the same server — acceptable for a power-user feature installed deliberately on the shared proxy.
- **`--name` is odda's key.** `install` refuses without `--force` if the name is taken (same semantics as `request clone`/`new`). With `--force`, odda removes the existing live instance first (firing its `done`), then execs and adds the new source — so mitmproxy's duplicate-`.name` `AddonManagerError` never surfaces.

## Capture honesty (chain order)

odda's own `FlowFileAddon` (which writes captured flows to `.odda/flows/`) is added to the addon chain **before** user proxy-scripts. mitmproxy fires hooks in chain order, so for the `request` event: `FlowFileAddon` writes the **original** request to disk first, then the user proxy-script's `request` hook runs and can mutate `flow.request`. For `response`: `FlowFileAddon` writes the response + `flows.jsonl` first, then the proxy-script sees it.

Consequence: captured `.odda/flows/<id>/request` files record what the client sent; a proxy-script's mutations affect what goes upstream, not what is captured. This is the desired split for a capture tool — you record what came in, you can still transform what goes out.

## Failure model

- **Exec failure** (syntax error, import error) at install or boot: the install is rejected (or, at boot, the proxy-script is skipped) and the error is logged via mitmproxy's `script_error_handler`. One bad proxy-script never blocks the server — the rest of the chain comes up.
- **Runtime hook error**: mitmproxy's `safecall()` logs the exception and the proxy continues. The flow proceeds (or fails gracefully depending on where in the pipeline the error happened). Discover errors via `odda logs`. `proxy-script list` shows no error state — the addon is still loaded, it just threw once.
- **Options.** A proxy-script can register options via `load(loader)` → `loader.add_option(...)`, which mutate the shared `DumpMaster.options`. Full mitmproxy parity — namespace your option names to avoid collisions with odda's own options or other proxy-scripts.

## Security

A proxy-script runs in the odda **server process** with full Python privileges: file access, network access, subprocess execution, and the ability to crash the proxy or server. This is not a new trust boundary for an OpenCode agent that already runs shell commands with full privileges — it's the same one. The risk is documented here, not gated. Do not install a proxy-script from a source you haven't read.