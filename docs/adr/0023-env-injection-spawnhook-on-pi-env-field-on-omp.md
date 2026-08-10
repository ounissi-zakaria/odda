# Env injection: `spawnHook` on pi, `event.input.env` on omp — two mechanisms, two files

Status: superseded by ADR 0024 (`process.env` inheritance, one mechanism, one file).

The pi/omp extension must make `ODDA_SOCKET`, `ODDA_DATA_DIR`, and
`ODDA_LOG` available to the agent's `odda` shell invocations. OpenCode has
a dedicated `shell.env` hook for this; pi/omp have no equivalent — env vars
must be folded into the bash tool before execution.

Three mechanisms exist across the two harnesses, and the clean one
**diverges** by harness:

- **`createBashTool({ spawnHook })`** (pi only) — the cleanest pattern,
  shown in pi's `examples/extensions/bash-spawn-hook.ts`. The `spawnHook`
  runs at spawn time (after the transcript records the command, before the
  shell executes), adjusting `command`/`cwd`/`env` with zero transcript
  pollution. omp does not export `createBashTool`; its bash tool is a class
  with no `spawnHook`.
- **`tool_call` handler setting `event.input.env`** (omp only) — omp's
  bash schema accepts `env?: Record<string,string>` and the wrapper honors
  a returned `{ input }` to replace execution input. pi's bash schema is
  `{ command, timeout? }` with no `env` field, and pi's executor
  destructures only `{ command, timeout }` — a mutated `env` is silently
  dropped. pi's `ToolCallEventResult` also has no `input` field.
- **`tool_call` handler prepending to `event.input.command`** (both) —
  works on both but pollutes every bash transcript entry.

We used the **clean per-harness mechanism**: `plugin-pi.ts` re-registered
bash via `createBashTool({ spawnHook })` with `env: { ...env, ODDA_SOCKET,
ODDA_DATA_DIR, ODDA_LOG }`; `plugin-omp.ts` used a `tool_call` handler that
returned `{ input: { ...event.input, env: { ...event.input.env, ODDA_* } } }`,
honored by omp's wrapper (no transcript pollution — the `env` field is a
structured input, not part of `command`). See ADR 0022 for why we shipped two
files instead of one with a runtime branch.

This was superseded by ADR 0024: the plugin switched to `process.env`
inheritance (set the vars at load time, let the harness's session env carry
them into every bash child), so neither `spawnHook` nor `event.input.env`
mutation is used on either harness anymore.

Rejected: (i) one file using `tool_call` command-prepend on both — the
lowest-common-denominator, but pollutes every bash transcript entry where a
clean mechanism exists. (ii) one file with a runtime branch — the
`createBashTool` import doesn't resolve on omp (the symbol doesn't exist),
so the branch can't be imported cleanly without dynamic imports. (iii)
re-registering bash via `createBashTool` on omp — the factory doesn't exist
there.

This is recorded because the two files look like duplication of the same
integration, but they're using genuinely different mechanisms for the same
goal, and a future contributor who "unifies" them will silently break one
harness. The invariant is: each harness uses its cleanest env-injection
mechanism; the shared part is the skill and the install command, not the
plugin source.