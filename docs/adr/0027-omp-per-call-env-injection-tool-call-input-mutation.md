# omp plugin: per-call env injection via `tool_call` input mutation, separate from pi

Status: accepted (supersedes ADR 0024's one-source decision for the plugin; the per-harness skill-dir decision stands). The whole plugin surface was later deleted by the MCP migration (v0.42.0): omp inherits MCP servers from IDE configs (or registers via its own config), spawning `odda mcp`; there is no socket and nothing to inject.

ADR 0024 shipped one extension source (`plugin-pi.ts`) for both pi and omp,
injecting `ODDA_*` via `process.env` inheritance: the plugin writes the vars
on `process.env` at `session_start`, and both harnesses build the bash session
env from `Bun.env`, so child processes inherit them. On omp this proved
unstable in practice: main-session bash calls intermittently ran without the
vars (observed: `printenv ODDA_SOCKET ODDA_LOG ODDA_DATA_DIR` → all unset,
exit 1, while other sessions in the same process passed). The inheritance
path depends on the harness's session-env construction, which is outside the
plugin's control.

The omp plugin now injects env **per call**: a `tool_call` handler on
`event.toolName === "bash"` returns
`{ input: { ...event.input, env: { ...(event.input.env ?? {}), ODDA_SOCKET, ODDA_LOG, ODDA_DATA_DIR } } }`.
omp's wrapper honors a returned `{ input }` as the replacement execution
input (revalidated against the schema, and the user approves what actually
runs), so every model-issued bash call carries the vars deterministically —
no dependence on session-env propagation. The native bash tool is never
replaced: its dynamic description, approval pattern rules, concurrency, and
rendering all stay intact. The plugin makes **no `process.env` writes at
all**; `session_start` still spawns the server, deduped by a socket-liveness
probe (a live server answers a connect; a stale socket file fails it and the
spawn rebinds — `server.py` unlinks the stale file). Accepted race: two
concurrent `session_start`s in one process could both pass the probe and
double-spawn, orphaning the first server; judged unreachable in practice
(the env gate in ADR 0024 was race-free because the set was synchronous).

pi keeps `plugin-pi.ts` with `process.env` inheritance (ADR 0024's mechanism):
pi's `ToolCallEventResult` has no `input` field (only in-place
`event.input` mutation, which pi's executor drops for `env`), so the omp
mechanism does not port. Two files again — the ADR 0022/0023 invariant
returns: each harness uses its cleanest env-injection mechanism; the shared
part is the skill and the install command, not the plugin source.

Rejected: (i) `pi.registerTool` re-registering `bash` with
`ctx.invokeTool` delegation — works on omp (same-tool delegation to the
native built-in exists there), but the wrapper *replaces* the native bash in
the registry, silently losing its dynamic description getter, pattern-based
approval rules, pty concurrency, and custom rendering; the user's
requirement was "same signature, description, and everything as the default
bash tool", which only the input-mutation approach satisfies. (ii) keeping
one source with a runtime branch — the mechanisms now genuinely diverge
again, and the `tool_call` result shape differs across the fork. (iii)
`user_bash` coverage for user-typed `!` commands — the event has no `env`
field to mutate; the only mechanism (command-prepend) is the transcript
pollution ADR 0023 rejected. Accepted gap: `!` commands get no `ODDA_*` env.

Install targets (unchanged):
- pi:  extension → `~/.pi/agent/extensions/odda.ts` (from `odda.harness.pi/plugin-pi.ts`),  skill → `~/.pi/agent/skills/odda/`
- omp: extension → `~/.omp/agent/extensions/odda.ts` (from `odda.harness.omp/plugin-omp.ts`), skill → `~/.omp/agent/skills/odda/`

This is recorded because "why two plugin files again after ADR 0024 unified
them?" is the obvious question, and the answer (process.env inheritance was
unstable on omp; the deterministic per-call mechanism exists only on omp and
must not replace the native bash tool) is not visible without reading both
forks' tool-call result shapes and bash tool internals.
