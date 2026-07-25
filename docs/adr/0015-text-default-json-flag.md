# CLI defaults to text output; `--json` opts into structured output

All odda CLI commands print human-readable text by default and accept
a global `--json` flag to emit structured JSON instead. Text output is
rendered by hand-written per-command formatters (tables for lists,
`key: value` for flat dicts, raw lines for `logs`, YAML-ish blocks for
`wrap dump` / `logpoint dump`, an indented per-script summary for
`coverage snapshot` / `coverage stop`); `eval` renders strings bare and
non-strings as compact JSON (the server returns parsed values from
Playwright, not JSON strings, so `eval`'s text form is readable rather
than a verbatim JSON pass-through). Errors print as `Error: <message>`
on stderr with a non-zero exit code in text mode (the `Server error
(-NNNN):` JSON-RPC prefix is stripped); `--json` mode keeps the
`{"error": ...}` shape on stdout. `--json` is a single global flag on
the root callback alongside `--socket` and `--data-dir`, inherited by
every subcommand.

Structured output (the `--json` form) is the parse target for scripts
and agents doing structural queries (notably `wrap dump` / `logpoint
dump` / `coverage snapshot` / `coverage stop`, whose records are nested
arrays a flat text format cannot express). It is stable by convention —
we will not break field names or shapes casually — but there is no
versioned schema contract; the package version is the only version.

This is a hard break, recorded for the same reason as ADR-0009: the
output contract is a public API, and a future contributor reading the
CLI might "helpfully" re-add JSON-everywhere and reopen the friction
this removed. The previous default (JSON for every command, errors as
JSON on stdout) was the second CLI-surface friction point after
positional args: every test doc carried `python3 -c 'import json,sys;
...'` to extract a single field, and agents paid token cost parsing
JSON they could have read as text. The text default removes both —
tests match rendered text directly, agents read tables and `key:
value` lines. The trade-off is a per-command renderer (~15 small
functions) and a split consumer contract (text drifts freely;
structured output is the stable parse target).