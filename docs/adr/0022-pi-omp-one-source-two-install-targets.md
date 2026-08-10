# pi + omp plugins: two extension sources, per-harness skill dirs, one install command

Status: superseded by ADR 0024 (one source, `process.env` inheritance).

pi (earendil-works/pi) and omp (can1357/oh-my-pi, "omp.sh") are the same
harness family: omp is a fork of pi-mono by Mario Zechner, rewritten and
extended by Can Bölük. Both share the Agent Skills standard for skills and
the same TypeScript extension contract (a module exporting a default factory
that receives `ExtensionAPI`).

The original plan was one extension source file shared across both
harnesses, diverging only at the install target. Investigation revealed a
load-bearing divergence in the bash tool: pi exports `createBashTool({ spawnHook })`
(a factory that re-registers the bash tool with a spawn-time `command`/`cwd`/`env`
hook — the clean env-injection mechanism shown in pi's
`examples/extensions/bash-spawn-hook.ts`); omp removed `createBashTool` and
`spawnHook` entirely — its `BashTool` is a class with an empty
`BashToolOptions` interface and no env-injection hook. The only env-injection
mechanism that worked on both was `tool_call` event mutation of
`event.input.command` (prepend `export ODDA_*\n`), which worked but polluted
every bash transcript entry and ignored omp's clean `event.input.env` field.

At the time, we shipped **two** extension source files (`plugin-pi.ts` using
`createBashTool({ spawnHook })`, `plugin-omp.ts` using `tool_call` env
injection) and **one** shared skill source (in `src/odda/harness/skill/`)
installed **per harness** to `~/.pi/agent/skills/odda/` (pi) and
`~/.omp/agent/skills/odda/` (omp) — each harness's first-party skill dir,
not the shared `~/.agents/skills/` location. One install command,
`odda install <pi|omp|opencode>`, dispatched to the matching plugin file
and skill path. See ADR 0023 for the env-injection mechanism details.

This was superseded by ADR 0024: omp later shipped a legacy-pi-coding-agent
compat shim that rewrites `@earendil-works/pi-coding-agent` imports, and the
plugin switched to `process.env` inheritance (no bash re-registration on
either harness), making the `createBashTool`/`spawnHook` divergence moot.
The per-harness skill-dir decision still stands.

Install targets:
- pi:     extension → `~/.pi/agent/extensions/odda.ts`,  skill → `~/.pi/agent/skills/odda/`
- omp:    extension → `~/.omp/agent/extensions/odda.ts`, skill → `~/.omp/agent/skills/odda/`
- opencode: plugin → `~/.config/opencode/plugins/odda.js`, skill → `~/.config/opencode/skills/odda/`

Rejected: (i) one extension source with a runtime harness-detection branch
— works, but the `createBashTool` import would fail to resolve on omp (the
symbol doesn't exist), so the branch can't even be imported cleanly without
dynamic imports or try/catch around the import; two files is simpler. (ii)
one source using `tool_call` command-prepend on both — the lowest-common-
denominator that works uniformly, but pollutes every bash transcript entry
on pi where a clean `spawnHook` exists, and ignores omp's `event.input.env`.
The user explicitly preferred each harness getting its cleanest mechanism.
(iii) shared `~/.agents/skills/odda/` skill dir for both pi and omp — both
harnesses discover it, but it couples the two harnesses' install/uninstall
lifecycles and litters the shared location if only one harness is used.
Per-harness skill dirs keep each install self-contained in its own config
tree; the skill *source* is shared in the repo, the install *target* is not.

This is recorded because "why two plugin files for the same harness family?"
and "why not the shared `~/.agents/skills/` for the skill?" are the obvious
questions, and the answers (the clean env-injection mechanism diverged across
the fork; per-harness skill dirs keep installs self-contained) are not
visible without reading both forks' bash tool source and skill discovery
paths.