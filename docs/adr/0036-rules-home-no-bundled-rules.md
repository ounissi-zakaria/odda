# Rules live only as slots in the config home; the package ships none

No Rules ship with odda. The Scanner boots with an empty ruleset, and a
fresh install scans nothing until rule slots arrive — a deliberate,
recorded state, not an omission. Every Rule — the maintainer's corpus
included — is a slot in the global rules dir under the odda config home
(`~/.config/odda/rules`, redirected by `ODDA_CONFIG_HOME`), installed
through `rule_install`, persisted across sessions, and loaded on boot
with bad slots contained. This supersedes the builtins-ship-with-odda
half of ADR-0035; that decision's core — rules are typed Python
objects in a stable author surface, not a DSL — stands unchanged.

The converted corpus (disclosure, fingerprint, cookie, takeover,
supply-chain, and request families, plus the retire.js adapter) lives
as installable slot sources at `tests/e2e/fixtures/rules/` — one slot
directory per family, test-local material outside the shipped package
(setuptools packages `src/` only). The corpus is source material for
installs, not a distribution: nothing auto-installs it, and whether it
returns as a separate distribution is a later maintainer decision. The
retire.js dataset rides in its slot directory as a flat asset next to
`rule.py`; installing from a file path copies the file's non-Python
siblings into the installed slot, so dataset-backed rules arrive
self-contained and stay offline and editable in place.

Slots that lived in the old per-data-dir `rules/` tree are not
migrated. The passive scanner is unshipped pre-release work: there is
no compatibility surface to preserve, and a migration path would
freeze a layout the config home already replaces.

The decision is recorded because "a scanner with no rules" reads as a
regression — the previous behavior found findings out of the box — and
because a rule corpus anywhere in the tree can read as bundling
without its reason; keeping it test-local makes its purpose
unambiguous. The scanner's value is the machinery: capture-time
observation, arrival-time semantics, the findings index. Curation of
what fires is a maintainer act, performed out of band through the same
install surface every rule uses.
