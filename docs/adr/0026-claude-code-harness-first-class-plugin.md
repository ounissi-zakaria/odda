# Claude Code harness ships as a first-class plugin bundle

Status: accepted (reverses ADR 0025's deferral of the first-class-plugin option; 0025's core "CLI + skill, not MCP" decision stands). The whole plugin bundle was later deleted by the MCP migration (v0.42.0): Claude Code registers `odda mcp` as a standard MCP server from its own config; no plugin, hook, or install command ships.

ADR 0025 chose CLI + skill (not MCP) for the Claude Code harness and deferred the first-class plugin + marketplace option as "a new packaging surface diverging from `odda install` parity." Reading the actual Claude Code plugin docs reversed that deferral: the first-class plugin model is the better fit for a distributable tool, and it removes the most intrusive part of the original install — mutating the user's global `~/.claude/settings.json`.

The harness is now a self-contained plugin bundle that `odda install claude` writes to `~/.claude/skills/odda/`:

```
.claude-plugin/plugin.json   # name, version (from odda.__version__), description
hooks/hooks.json              # SessionStart -> python3 ${CLAUDE_PLUGIN_ROOT}/scripts/start.py (exec form)
scripts/start.py              # starts the server, writes ODDA_* into $CLAUDE_ENV_FILE (Python; not detached)
skills/odda/…                 # the shared skill (SKILL.md + reference .md + recipes/)
```

Claude Code auto-loads any directory under `~/.claude/skills/` that contains a `.claude-plugin/plugin.json` as a "skills-directory plugin" (`odda@skills-dir`) — no marketplace, no install step beyond `odda install claude`. The `SessionStart` hook lives in the bundle's `hooks/hooks.json` and references `start.py` via `${CLAUDE_PLUGIN_ROOT}`, so no path is hardcoded and the user's `settings.json` is never touched. `odda install claude` remains the single installer entry point (parity with opencode/pi/omp); only its artifact changed.

## Considered options

- **First-class plugin bundle (adopted)** — `hooks/hooks.json` for a self-contained `SessionStart` hook (no `settings.json` mutation); `skills/` for a namespaced, versioned skill; skills-directory auto-load so no marketplace is needed. Best fit for a distributable tool and the least intrusive. The cost is a small packaging surface (`plugin.json` + `hooks.json`, generated inline at install time so the version tracks `odda.__version__`) and the skill's slash name becomes `/odda:odda` (plugin namespace) instead of `/odda`. `odda install claude` stays the installer for parity.
- **Standalone `.claude/` config (the previous approach — ADR 0025 as shipped)** — copy `start.sh` to `~/.claude/odda/` and merge a `SessionStart` hook into the user's global `~/.claude/settings.json`. Simpler artifact, but mutates a shared global config the user owns (the most intrusive part), and the skill sat as a loose, unversioned copy in `~/.claude/skills/odda/`. Replaced.
- **MCP tools / first-class plugin + marketplace** — still rejected for the same reasons as ADR 0025 (new core surface / release plumbing diverging from `odda install`). The skills-directory auto-load mechanism is what makes the first-class plugin viable without a marketplace.

## Consequences

- **No `settings.json` mutation.** The hook lives in the bundle; the user's `~/.claude/settings.json` is never touched. The pre-plugin install (commit `33d473b`, never released) wrote a hook into `settings.json` and a script into `~/.claude/odda/`; that footprint was removed once by hand rather than carried as installer migration code, since no released version had it.
- **Skill invocation is namespaced** as `/odda:odda` (plugin `odda` + skill folder `odda`). Model-invoked discovery is unaffected (description-driven); only the explicit slash name changed from `/odda`.
- **No `bin/` shim.** `bin/` is added only to the Bash tool's PATH while the plugin is enabled; the `SessionStart` hook is not a Bash-tool call, so `bin/` wouldn't help the server-start path. It would only marginally help Bash-tool `odda` calls for the venv-only-not-on-PATH edge case, at the cost of a fragile path-baked wrapper, and would diverge from opencode/pi/omp (which all assume `odda` on PATH). The "odda must be on PATH" caveat from ADR 0025 stands, identical to the other harnesses.
- **`start.py` is Python, not shell.** The hook is a standalone Python script invoked as `python3 …/start.py` (exec form, no shell-quoting risk). It does not detach the server into a new session (no `setsid`), matching opencode's `detached: false`: the server is a child of the short-lived hook process and reparents to init when the hook exits, so it outlives the hook without blocking it; `SIGHUP` is ignored in the hook before the spawn so the inherited disposition (preserved across exec) protects the server from a group HUP. The hook's runtime is the same `python3` odda already requires, so it adds no dependency; it gains `shlex.quote` for the env-file values and a native `AF_UNIX` liveness probe instead of a hand-rolled shell quoting helper and an inline-Python heredoc. `CLAUDE_PID`/`CLAUDE_ENV_FILE`/`CLAUDE_PROJECT_DIR` are available to plugin hooks exactly as to `settings.json` hooks (plugin hooks merge with user hooks and run identically). The server lifetime (`--parent-pid`, auto-die) is unchanged.
- **ADR 0025** — its core "CLI + skill, not MCP" decision stands; only its "first-class plugin — Deferred" option is reversed here. The accepted gap (session modes that don't source `$CLAUDE_ENV_FILE`) and the planned PPID-walk remedy are unchanged.