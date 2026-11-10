# Rule slots live in two scopes; the package ships none

No rules ship with odda. The Scanner boots with an empty ruleset, and
a fresh install scans nothing until rule slots arrive — a deliberate,
recorded state, not an omission. Every rule — the maintainer's corpus
included — is a slot directory holding one `rule.py`, persisted across
sessions and loaded on boot with bad slots contained. This supersedes
the builtins-ship-with-odda half of ADR-0035; that decision's core —
rules are typed Python objects in a stable author surface, not a DSL —
stands unchanged. It also supersedes this ADR's own earlier shape
(slots in the config home only): the per-data-dir `rules/` tree it
removed returns as one of two scopes, deliberately different from the
single-home layout it replaced.

Slots live in two scopes. Project slots persist in the data dir's
`rules/` tree — per-project like flows, findings, and proxy-scripts —
and are the only slots `rule_install` and `rule_remove` manage: the
tools that mutate what fires act on the project the session owns,
never on machine-global state. Global slots persist in the config
home's rules dir (`~/.config/odda/rules`, redirected by
`ODDA_CONFIG_HOME`), change only by hand, and load in every project on
the machine — the shared-corpus path. Both scopes load on boot, and
rule ids are unique across both because they alone key the
append-only findings index. `rule_list` labels every row with its
scope.

Disabling completes the surface: a project suppresses an installed
rule — a whole slot by name, one rule by id — with
`rule_disable`/`rule_enable`, persisted as entry files under
`.odda/scanner/disabled/` (presence = disabled; one file per entry,
written by temp-and-rename, no lock protocol — sibling sessions racing
on distinct entries touch distinct files). A disabled rule stays
installed and listed; it is withheld from the live registration, so
disable and enable ride the same arrival-time epochs as install and
remove (ADR-0034) and cost no scan time. A slot remove takes the
disable entries it empties, so a fresh install of the same name starts
active; a `force` overwrite keeps a slot's name-keyed disable across
the swap.

Slots that lived in earlier layouts are not migrated. The passive
scanner is unshipped pre-release work: there is no compatibility
surface to preserve, and a migration path would freeze layouts the
two-scope split replaces.

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

The decision is recorded because "a scanner with no rules" reads as a
regression — the previous behavior found findings out of the box —
because a rule corpus anywhere in the tree can read as bundling
without its reason; and because project-only install/remove beside
hand-managed global slots can read as a missing feature rather than a
trust boundary: the mutation tools act only on the project the session
owns, and everything machine-global is a deliberate out-of-band act.
The scanner's value is the machinery: capture-time observation,
arrival-time semantics, the findings index. Curation of what fires is
a maintainer act, performed out of band through the same install
surface every rule uses.
