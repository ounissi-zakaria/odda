# Scanner rules are Python registrations, not a rule DSL

Rules are typed Python objects: builtins are literals (a match spec plus
gates — scheme, status, content type, browser attribution) grouped in
modules under `odda.scanner`, and user rules are Python files persisted
under the data dir and re-loaded on session boot, importing the same stable
rule types. There is no YAML/JSON rule format, no per-rule plugin files,
and no interpreter to grow. Rule ids are permanent (they key findings
across the append-only index); a user rule whose id collides with a
builtin or another user rule is rejected at install; removing a rule stops
future findings without retracting past ones.

A nuclei-style DSL was rejected on three counts. It would require growing
an interpreter forever (condition trees, helper functions, scoping,
versioned schema) for a ruleset of ~15; its one real advantage —
distributing rules from untrusted parties without executing code — is void
here, because `proxy_script_install` already persists and execs arbitrary
Python from source under the same trust boundary; and the
ecosystem-corpus argument is better served by exporting flows for the real
nuclei binary than by reimplementing a partial template engine. One-file-
per-rule was rejected as boilerplate plus discovery machinery plus an
implicit per-file ABI. The declarative mass that would justify a data
format comes from datasets instead, consumed in their native formats
behind the same Rule interface: retire.js `jsrepository.json` now; gitleaks
TOML, enthec fingerprints, and OSV version→CVE lookups later.

Builtins also carry a deliberately high signal bar, recorded here so it
survives: a builtin rule must be actionable when it fires — specific,
rare, and exploitable or leakable (stack traces, private keys, takeover
confirmations, Xdebug session echoes, cleartext credentials,
version-mapped CVEs). Hygiene checks — missing security headers, cookie
attribute flags, version banners without a CVE mapping, internal-IP and
email disclosure — are not builtins; they are one user-rule away for
anyone who wants the hygiene tier, and the omission is the design.

The decision is recorded because the DSL question recurs every time
someone meets nuclei templates, and the absent CSP check looks like an
omission rather than a bar — without this record, both pull toward an
interpreter and a noise profile the scanner was explicitly shaped to
avoid.
