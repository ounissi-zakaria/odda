# Wraps and userscripts are per-browser-scoped, not globally shared

Wraps and userscripts are stored per-browser: each `BrowserInstance`
owns its own userscripts directory on disk and its own userscript
extension instance. A wrap or userscript installed on browser 1 does
not reach browser 2. The `--browser-id` argument on `userscript
install/remove`, `wrap calls add`/`access add`/`list`/`remove`, and
the implicit browser binding of built-in default userscripts now
selects the *scope* the script lives in, not just which browser to
reload the shared extension on.

This reverses the original model where all browsers shared one
on-disk `userscripts/` directory and one extension load. That model
was simpler (one dir, one `sync_extension`) but leaked state across
browsers: an agent opening a fresh browser inherited every wrap from
previous sessions, polluting their results (reported by 2 of 13
agents in the feedback). The leak directly contradicts odda's
multi-agent safety model, where every other per-target state (tab
IDs, coverage recording, logpoint installations) is already
per-browser or per-tab.

The trade-off is that an agent who wants the same userscript on
every browser must install it N times, and the `userscript`/`wrap`
modules now carry a `browser_id` parameter through their install,
remove, and list paths instead of operating on a single global dir.
We accept that because wraps are the dynamic-analysis tool that
multiple agents run simultaneously; isolating their state matches
the rest of odda's targeting model. Plain userscripts follow wraps
for one-mechanism-one-scope-rule consistency, even though their leak
was less harmful.

The decision is recorded because it changes the meaning of
`--browser-id` on the affected commands from a reload trigger to a
scope key — a future reader who knew the old model would find the
per-browser dirs surprising, and a contributor might "simplify" it
back to the shared dir, reintroducing the leak.