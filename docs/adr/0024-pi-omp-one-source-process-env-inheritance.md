# pi + omp plugins: one extension source, per-harness skill dirs, one install command

Status: accepted (supersedes ADR 0022's two-file decision)

pi (earendil-works/pi) and omp (can1357/oh-my-pi, "omp.sh") are the same
harness family: omp is a fork of pi-mono by Mario Zechner, rewritten and
extended by Can Bölük. Both share the Agent Skills standard for skills and
the same TypeScript extension contract (a module exporting a default factory
that receives `ExtensionAPI`).

ADR 0022 shipped two extension source files because the env-injection
mechanism diverged across the fork: pi had `createBashTool({ spawnHook })`,
omp did not. That divergence no longer matters — the plugin no longer
re-registers bash on either harness. Instead it sets `ODDA_SOCKET` and
`ODDA_LOG` on `process.env` at extension load time (before any bash call),
and `ODDA_DATA_DIR` inside `startServer` (it is cwd-dependent, so it cannot
be set until `session_start` supplies the cwd). Child-process inheritance
carries all three into every bash child via the harness's session env. Both
pi and omp build the bash session env from `Bun.env` (pi:
`filterChildShellEnv(Bun.env)` in `buildSpawnEnv`; omp: the same path
through `procmgr.getShellConfig`), so a `process.env` write reaches the
bash tool's spawned children without re-registering the tool, without a
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