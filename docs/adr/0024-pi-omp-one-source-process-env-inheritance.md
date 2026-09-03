# pi + omp plugins: one extension source, per-harness skill dirs, one install command

Status: superseded by ADR 0027 for omp (per-call injection). The whole plugin surface was later deleted by the MCP migration (v0.42.0): harnesses spawn `odda mcp` from their own MCP config; there is no socket and nothing to inject.

pi (earendil-works/pi) and omp (can1357/oh-my-pi, "omp.sh") are the same
harness family: omp is a fork of pi-mono by Mario Zechner, rewritten and
extended by Can Bölük. Both share the Agent Skills standard for skills and
the same TypeScript extension contract (a module exporting a default factory
that receives `ExtensionAPI`).

ADR 0022 shipped two extension source files because the env-injection
mechanism diverged across the fork: pi had `createBashTool({ spawnHook })`,
omp did not. That divergence no longer matters — the plugin no longer
re-registers bash on either harness. Instead it sets `ODDA_SOCKET` and
`ODDA_LOG` on `process.env` inside `startServer` (the first
`session_start` that spawns the server), alongside `ODDA_DATA_DIR` (which is
cwd-dependent, so it cannot be set until `session_start` supplies the cwd).
Child-process inheritance carries all three into every bash child via the
harness's session env. Both pi and omp build the bash session env from
`Bun.env` (pi: `filterChildShellEnv(Bun.env)` in `buildSpawnEnv`; omp: the
same path through `procmgr.getShellConfig`), so a `process.env` write reaches
the bash tool's spawned children without re-registering the tool, without a
`tool_call` handler, and without transcript pollution. `ODDA_DATA_DIR`'s
late write is safe because `startServer` runs on `session_start` and awaits
the socket before the handler returns — no bash child runs before it
completes.

omp additionally ships a legacy-pi-coding-agent compat shim
(`src/extensibility/legacy-pi-coding-agent-shim.ts`) whose plugin loader
(`legacy-pi-compat.ts`) rewrites `@earendil-works/pi-coding-agent` imports to
`@oh-my-pi/pi-coding-agent`. The single `plugin-pi.ts` imports
`ExtensionAPI` from `@earendil-works/pi-coding-agent`; on omp that import
resolves through the shim. One source file installs to both harnesses.

We ship **one** extension source file (`plugin-pi.ts`) and **one** shared
skill source (in `src/odda/harness/skill/`) installed **per harness** to
`~/.pi/agent/skills/odda/` (pi) and `~/.omp/agent/skills/odda/` (omp) —
each harness's first-party skill dir, not the shared `~/.agents/skills/`
location. One install command, `odda install <pi|omp|opencode>`,
dispatches to the matching plugin file and skill path.

Install targets:
- pi:     extension → `~/.pi/agent/extensions/odda.ts`,  skill → `~/.pi/agent/skills/odda/`
- omp:    extension → `~/.omp/agent/extensions/odda.ts`, skill → `~/.omp/agent/skills/odda/`
- opencode: plugin → `~/.config/opencode/plugins/odda.js`, skill → `~/.config/opencode/skills/odda/`

Rejected: (i) keep two files (ADR 0022) — the `createBashTool`/`spawnHook`
divergence that motivated them is irrelevant once neither harness
re-registers bash; `process.env` inheritance works on both. (ii) one source
using `tool_call` command-prepend on both — pollutes every bash transcript
entry where a clean inheritance path exists. (iii) shared
`~/.agents/skills/odda/` skill dir for both pi and omp — both harnesses
discover it, but it couples the two harnesses' install/uninstall lifecycles
and litters the shared location if only one harness is used. Per-harness
skill dirs keep each install self-contained in its own config tree; the
skill *source* is shared in the repo, the install *target* is not.

This is recorded because "why one plugin file for two harnesses that
diverged?" and "why not the shared `~/.agents/skills/` for the skill?" are
the obvious questions, and the answers (env injection via `process.env`
inheritance makes the bash-tool divergence moot; per-harness skill dirs keep
installs self-contained) are not visible without reading both forks' bash
executor env construction and skill discovery paths.

## One server per process, reused across sessions

The socket path is derived from `process.pid`, and every session inside one
harness process loads `plugin-pi.ts` as a fresh module instance. A
per-instance `started` flag therefore cannot dedupe across sessions: the
second session would spawn a second `odda server` on the same path, which
`server.py` would previously unlink-and-rebind, orphaning the first server
(browsers, tabs, and flows became unreachable while its process lingered).

Instead the plugin gates the spawn on `process.env.ODDA_SOCKET !== socketPath`:
`ODDA_SOCKET` is set inside `startServer` (a pid-derived path), so it is
absent until the first `session_start` spawns the server and present
thereafter. `process.env` is process-wide, so it survives the per-session
module reload that defeats a module-level `started` flag — it is the
cross-instance lock. The equality check, not mere truthiness, rejects an
inherited parent process's `ODDA_SOCKET`, which carries a different
pid-derived path: a child harness process inherits the parent's env but
must start its own server, not reuse the parent's. The server is owned by
the harness process (it dies with `--parent-pid`), so every `session_start`
may attempt the spawn; the env gate dedupes subsequent ones within the
process. The shared server shuts down with the harness process
(`--parent-pid`).

An earlier version of the plugin gated the spawn on `session_start`'s
`reason === "startup"` (pi's event reasons: `"startup" | "reload" | "new" |
"resume" | "fork"`). That gate is not portable to omp: omp's
`SessionStartEvent` (`@oh-my-pi/pi-coding-agent`'s `shared-events.d.ts`)
carries only `type: "session_start"` — no `reason` field — so every omp
`session_start` fired with `reason === undefined` and the server never
started. The legacy-pi-compat shim rewrites the import path but not the
event payload shape. The env gate is harness-agnostic because it depends
only on `process.env`, not on event-field conventions that diverge across
the fork.