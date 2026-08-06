# Proxy-scripts

`odda proxy-script` manages Python files in mitmproxy `-s` script format that odda adds to the running proxy's addon chain. Install one to intercept, modify, record, or generate HTTP traffic at the proxy layer — before it reaches the browser or the upstream server. This is the reference for the file format, scope, and failure model; see [SKILL.md](SKILL.md) for the command surface.

## Commands

- `odda proxy-script install --name <name> --file <path>` — Install a `.py` file as a proxy-script. Overwrites an existing proxy-script of the same name only with `--force`. Alternatively, use `--source "<py>"` for inline source (mutually exclusive with `--file`).
- `odda proxy-script list` — List installed proxy-scripts with their names and sizes.
- `odda proxy-script remove --name <name>` — Remove a proxy-script: deletes its on-disk source and removes the live addon from the proxy chain.

## File format (mitmproxy `-s` script format)

A file is a valid proxy-script if `mitmproxy -s <file>` accepts it. Hook methods are top-level functions in the file:

```python
def request(flow):
    flow.request.headers["x-custom"] = "injected"

def response(flow):
    if flow.response.status_code == 200:
        flow.response.headers["x-seen-by"] = "odda"
```

For composition, the file can expose an `addons = [...]` list of class instances instead — mitmproxy's addon manager recurses into it. Both forms work identically to `mitmproxy -s`.

The standard mitmproxy hooks apply: `request`, `response`, `error`, `load`, `running`, `configure`, `done`, `client_connected`, `server_connected`, etc. See the [mitmproxy addons documentation](https://docs.mitmproxy.org/stable/api/events.html) for the full event list. An addon can register and read its own options the same way a `mitmproxy -s` addon does.

## Storage and scope

- **Storage.** Source is persisted under `.odda/proxy-scripts/<name>/script.py` and re-added on server boot, so an agent's proxy-script survives an odda server restart (new OpenCode session, machine reboot).
- **Scope is global**, not per-browser. The proxy is one shared instance across all browsers, so a proxy-script sees **every** flow from every browser through the odda server. This differs from userscripts (per-browser). The cost is that an agent's proxy-script sees other agents' traffic through the same server — acceptable for a power-user feature installed deliberately on the shared proxy.
- **`--name` is odda's key.** `install` refuses without `--force` if the name is taken (same semantics as `request clone`/`new`). With `--force`, odda removes the existing live instance first, then loads and adds the new source.

## Capture honesty (chain order)

Captured `.odda/flows/<id>/request` files record what the client sent; a proxy-script's mutations affect what goes upstream, not what is captured. You record what came in, you can still transform what goes out.

## Failure model

- **Load failure** (syntax error, import error) at install or boot: the install is rejected (or, at boot, the proxy-script is skipped) and the error is logged. One bad proxy-script never blocks the server — the rest of the chain comes up.
- **Runtime hook error**: the exception is logged and the proxy continues. The flow proceeds (or fails gracefully depending on where in the pipeline the error happened). Discover errors via `odda logs`. `proxy-script list` shows no error state — the addon is still loaded, it just threw once.
- **Options.** A proxy-script can register its own options (full mitmproxy parity) — namespace your option names to avoid collisions with odda's own options or other proxy-scripts.

## Security

A proxy-script runs with full privileges: file access, network access, subprocess execution, and the ability to crash the proxy or server. This is not a new trust boundary for an OpenCode agent that already runs shell commands with full privileges — it's the same one. The risk is documented here, not gated. Do not install a proxy-script from a source you haven't read.