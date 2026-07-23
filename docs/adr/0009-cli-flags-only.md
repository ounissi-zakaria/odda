# CLI commands are flag-only — positional arguments are removed

All odda CLI commands accept their inputs as named flags only. The four
commands that previously took a positional argument (`navigate <url>`,
`eval <js>`, `browser close <id>`, `wrap remove <name>`, plus the
`page click/fill/hover/upload <ref>` family, `request clone <flow-id>`,
`request send <name>`, `userscript remove <name>`, `logpoint remove <id>`)
now require `--url`/`--js`/`--browser-id`/`--name`/`--ref`/`--flow-id`/
`--id` respectively. The positional forms are removed, not kept as
aliases — every existing example, script, and test doc was updated in
the same change.

This is a hard break because the positional-vs-flag inconsistency was
the second most-reported friction point in the agent feedback: 8 of 13
agents guessed wrong on at least one command and had to `--help` each
one. A mixed surface (flags added as aliases) would preserve the
inconsistency agents tripped on. The clean break unifies the surface at
the cost of one migration; the alternative (everything positional) was
rejected because flags are self-documenting in `--help` and in error
messages, which is the discoverability property that mattered most to
agents who had never seen odda before.

The decision is recorded because removing positional args after they
shipped is surprising — a future contributor reading the CLI might
"helpfully" re-add a positional for `navigate`'s URL and reopen the
inconsistency. This ADR says: don't.