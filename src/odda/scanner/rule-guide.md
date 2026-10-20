# Scanner rule guide

Captured flows are passively scanned while the session lives: every
flow the session writes is checked against the installed rules, and
matches append to `.odda/scanner/findings.jsonl` — one line per
rule-flow pair, evidence aggregated, never retracted. Nothing is ever
sent anywhere (the scanner reads captured material only), and flows
that predate the session are never scanned.

No rules ship with odda. Rules live in two scopes, both loaded on
every boot: this project's rules dir (`.odda/rules/`, what
`rule_install` and `rule_remove` manage, private to the project like
everything else in `.odda/`) and the global rules dir under the odda
config home (`~/.config/odda/rules`), which no tool writes — global
slots are placed and removed by hand and fire in every project on the
machine.

## Installing rules — arrival-time semantics

`rule_install` installs into the project's rules dir. It takes a slot
name plus the rule file's source text or a file path (pass exactly
one; `force` overwrites an existing slot by name). Installing from a
file path copies the file's sibling non-.py assets — datasets — into
the slot. Install validates before persisting: a file that fails to
load, or whose rule ids collide with another slot's in either scope,
is rejected naming the problem (and showing the file shape), and
nothing reaches disk.

Rules apply from arrival, not from scan time: a rule installed
mid-session matches flows captured after the install, never
retroactively — even a flow whose scan drains after the install.
Removing a slot (`rule_remove`) stops its future findings only; past
findings stay in the append-only index.

## Scopes, disabling, and the listing

Every installed rule belongs to one scope — `project` (`.odda/rules/`)
or `global` (the config home's rules dir, hand-managed) — and
`rule_list` shows one row per rule with its `scope` and `state`.
Rule ids are unique across both scopes: an install whose id already
lives in either one is rejected.

`rule_disable` suppresses an installed rule without removing it —
pass exactly one of `name` (every rule in that slot) or `id` (a
single rule); a name covers every slot of that name in both scopes.
Disabled rules stay installed and listed (`state: "disabled"`), stop
matching flows captured after the disable, and come back with
`rule_enable` (same params, and strict: disabling what is already
disabled or enabling what is not errors). Disables are project state:
they persist under `.odda/` and never touch the global rules dir, and
like every ruleset change they apply from arrival — findings already
recorded stay. Removing a project slot takes its disable entries with
it — an entry survives only while some rule it covers remains — so a
fresh install of the same name starts active; `force` keeps a slot's
name-keyed disable across the swap.

## The rule file

One slot is one directory holding one Python file (`rule.py`) whose
module-level `RULES` list defines the slot's rules. The file runs as
arbitrary Python under the same trust boundary as proxy-scripts, so it
can load datasets riding next to it. The shape:

```python
{{RULE_FILE_SKELETON}}
```

A rejected install repeats this shape with the exact problem named —
the same constant this guide carries, so the two cannot disagree.

## Rule fields — matchers and gates

A `Rule` declares matchers (regexes) and gates (flow context). Every
matcher that is set runs; each match becomes one evidence entry; a flow
must pass every gate that is set for any of the rule's matchers to
apply. Plain strings compile with `re.MULTILINE` (`^`/`$` match at
line breaks — frame-shaped patterns need that); precompiled
`re.Pattern` objects pass through untouched.

- `body_patterns` — matched against the stored response body. Each
  match becomes `Evidence(where="response_body", offset=<character
  offset of the match start>, detail=<matched text>)`.
- `header_patterns` — matched against the stored raw response headers
  (status line + header lines, `"Name: value"`). Evidence carries
  `where="response_headers"` and character offsets.
- `request_query_patterns` — matched against the request URL's query
  parameter *names*. A matching parameter becomes
  `where="request_query"` with the whole `name=value` pair as detail
  and the parameter's index as offset — the name is the signal, the
  value is the evidence.
- `request_header_patterns` — matched against the stored request
  header lines `"Name: value"` (value included — the credential itself
  is the evidence). `where="request_headers"`, offset is the header's
  index.

Gates — a flow must pass every gate that is set:

- `schemes` — request schemes only (`("http", "https")`); empty
  matches all.
- `statuses` — response status codes only; error flows (no response)
  never pass a status gate.
- `content_types` — prefixes against the response Content-Type
  (parameters stripped, lowercased): `("text/",)` matches
  `text/html; charset=utf-8`. Flows without a response have no content
  type and pass no content-type gate.
- `browser_only` — only flows that rode a browser's proxy credentials;
  crafted `request_send` probes are structurally invisible to these
  rules.
- `request_browser_only` — the browser gate scoped to the request-side
  matchers only, so one rule can mix halves: an ungated response
  matcher next to a request matcher that trusts browser-produced
  requests only.

`severity` and `title` ride onto every finding the rule records;
`refs` (CWE/CVE/OSV identifiers or URLs) ride along unchanged.

All matches of all matchers for one rule on one flow aggregate into a
single finding with one evidence list; matched text is capped at 200
characters per evidence entry.

## The severity scale

Findings carry one of five severities, which is the triage order:

- `info` — noteworthy, nothing to act on (inventory, fingerprints).
- `low` — hygiene debt, not exploitable on its own.
- `medium` — real exposure that needs triage (version disclosures,
  weak configurations).
- `high` — likely-exploitable exposure (leaked secrets, takeover
  confirmations).
- `critical` — drop-everything compromise risk.

Declarative rules carry one static severity; a `scan()` override may
choose severities per finding (the highest matched severity wins, the
dataset-rule shape below).

## Rule ids are permanent

Rule ids key the findings index — the pair (rule id, flow id) is a
finding's identity. Ids are forever: renaming a rule severs its
findings history, and findings already recorded never change.
Ids must be unique across every installed slot in both scopes; a
colliding install is rejected naming the collision. Pick a dotted
`<family>.<name>` id (like `example.backup-artifact`) and never
rename it once findings exist.

## scan() overrides — dataset-style rules

When a check cannot be expressed as patterns and gates — dataset
lookups, version comparisons, cross-referencing request and response —
subclass `Rule` and override `scan`:

- Return `None` and the declarative matchers and gates run (the
  default `Rule.scan` does exactly that).
- Return a list of `Finding` objects and those are recorded verbatim —
  an empty list means "no findings". A non-None return is
  authoritative: the declared matchers and gates do **not** run for
  that rule on that flow, so the override owns its own gating (see the
  content-type check in the example).
- The scanner calls `scan` once per arriving flow, on captured
  material only; an exception inside is contained per rule (logged,
  skipped, capture unaffected) — still, prefer checking for the
  material you need over relying on that.
- Keep evidence details greppable and bounded (~200 characters — the
  declarative path's cap).
- The dataset facet (`view.dataset_facet()`) is THE flow context a
  `scan()` override may gate on: the record's routing fields
  (`method`, `scheme`, `host`, `port`, `path`, `browser_id`,
  `status_code`, `error`), the stored response headers (so
  content-type screens work), and the body file's extension.
  Repeated bodies are memoized keyed on (ruleset, body bytes, facet;
  the cache resets whenever rules are installed or removed) — and a
  scan that reads beyond the facet
  (request-side material above all) is never memoized: it runs fresh
  on every flow, so a cached result can never answer for a flow it
  never saw.

The retired-library rule odda's own corpus uses (`js.outdated-library`)
is exactly this shape: a vendored dataset riding in the slot directory,
hash/name/content extractors inside `scan`, per-advisory severities
with the highest matched one winning.

### Declaring a body corpus

A dataset rule whose body check is "match these patterns" declares
the patterns instead of scanning them itself:

- Override `body_corpus()` to return the compiled patterns, in the
  order `scan` consumes them.
- The engine compiles them into the one body-stage database every
  rule's body patterns share — one GIL-released engine pass per body
  answers them all — and passes their spans to `scan` as a second
  argument: `def scan(self, view, body_spans)`. The argument arrives
  exactly when a corpus was declared; one entry per pattern, aligned
  with `body_corpus()`: that pattern's match spans over the body
  (identical to its `finditer`), or `None` when the pattern did not
  ride the engine pass (over-budget or engine-refused shapes) and
  must be scanned inside `scan`, behind its literal gates.

## FlowView — what a scan() override can read

`scan(self, view: FlowView)` receives one flow's captured material.
Each property is read from disk once, on first use, so a response-side
check never pays for the request side. Reading material the flow never
had (an error flow has no response side) raises `FileNotFoundError`.

- `record` — the flow's index record dict: `id`, `method`, `scheme`,
  `host`, `port`, `path`, `browser_id` (None for non-browser flows),
  `status_code` (None on error flows), `total_duration_ms`,
  `body_file` (relative stored-body path or None), `error` (message or
  None).
- `request` — `str`, the stored raw request (request line + headers +
  body). Raises `FileNotFoundError` when no request was stored.
- `request_method` — `str | None`, the request line's method token.
- `request_path` — `str | None`, the request line's target (path with
  query).
- `request_query` — `tuple[(name, value), ...]`, the request URL's
  decoded query pairs.
- `request_headers` — `tuple[(name, value), ...]`, the request's
  header pairs, in order.
- `response_headers` — `str`, the stored response headers (status line
  + headers). Error flows have none.
- `body` — `str | None`, the stored response body, leniently decoded
  (undecodable bytes become replacement characters). None when no body
  was stored.
- `body_bytes` — `bytes | None`, the raw stored body bytes — use for
  content hashing; never passes through text decoding.
- `content_type` — `str | None`, the response Content-Type header
  value; None when the flow has no response headers or no such header.
- `is_browser` — `bool`, whether the flow rode a browser's proxy
  credentials.

- `dataset_facet()` — `tuple`, the flow context a `scan()` override
  may lawfully gate on (see scan() overrides above); the scan-result
  memo keys on exactly it, and reads beyond it disable memoization
  for that scan — fresh scan per flow, correct findings on both
  sides of any body repeat.

## Worked example — declarative rule

```python
from odda.scanner import Rule, Severity

RULES = [
    Rule(
        id="example.backup-artifact",
        severity=Severity.HIGH,
        title="Database backup served from the web root",
        body_patterns=(
            r"-- PostgreSQL database dump",
            r"^INSERT INTO users \(.*password",
        ),
        content_types=("application/", "text/"),
        refs=("CWE-200",),
    ),
]
```

## Worked example — scan() override

A complete dataset-backed slot. The dataset
(`fingerprints.json`, one `{"software": ..., "pattern": ...,
"vulnerable_below": ..., "severity": ..., "refs": [...]}` object per
entry, each pattern with a `version` named group) installs alongside
this file when passed to `rule_install` as a file path:

```python
"""Dataset-backed slot: known-vulnerable software fingerprints."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from odda.scanner import Evidence, Finding, FlowView, Rule, Severity

# The dataset rides in this slot directory, next to this file.
# Installing from a file path copies the file's sibling non-.py
# assets into the slot, so the JSON arrives self-contained.
_SLOT_DIR = Path(__file__).resolve().parent

# Severity.rank is the scale's own triage order (info 0 … critical 4)
# — compare through it when the highest matched severity wins.


@dataclass(frozen=True)
class _Entry:
    software: str
    pattern: re.Pattern[str]
    vulnerable_below: str
    severity: Severity
    refs: tuple[str, ...]


def _load_entries() -> tuple[_Entry, ...]:
    document = json.loads((_SLOT_DIR / "fingerprints.json").read_text())
    return tuple(
        _Entry(
            software=item["software"],
            pattern=re.compile(item["pattern"]),
            vulnerable_below=item["vulnerable_below"],
            severity=Severity(item["severity"]),
            refs=tuple(item.get("refs", ())),
        )
        for item in document["entries"]
    )


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) if part.isdigit() else 0 for part in version.split("."))


class OutdatedSoftwareRule(Rule):
    """The inherited fields are the listing defaults; the override
    decides the severity of what it actually finds."""

    def scan(self, view: FlowView) -> list[Finding]:
        # The override owns its gates: anything declared on the Rule
        # (content_types, statuses, ...) does NOT apply here.
        mime = (view.content_type or "").split(";", 1)[0].strip().lower()
        if not mime.startswith("text/"):
            return []
        body = view.body
        if body is None:
            return []

        evidence: list[Evidence] = []
        refs: list[str] = []
        worst = Severity.INFO
        for entry in _load_entries():
            match = entry.pattern.search(body)
            if match is None:
                continue
            version = match.group("version")
            if _version_tuple(version) >= _version_tuple(entry.vulnerable_below):
                continue
            evidence.append(
                Evidence(
                    where="response_body",
                    offset=match.start(),
                    detail=(
                        f"{entry.software} {version} < {entry.vulnerable_below}"
                    )[:200],
                )
            )
            refs.extend(r for r in entry.refs if r not in refs)
            if entry.severity.rank > worst.rank:
                worst = entry.severity
        if not evidence:
            return []
        return [
            Finding(
                rule_id=self.id,
                flow_id=str(view.record["id"]),
                severity=worst,
                title=self.title,
                evidence=tuple(evidence),
                refs=tuple(refs),
            )
        ]


RULES = [
    OutdatedSoftwareRule(
        id="example.outdated-software",
        severity=Severity.HIGH,
        title="Known-vulnerable software version served",
    )
]
```

## Engine compatibility — the pattern-shape tradeoff

Body patterns of the live ruleset compile into one multi-pattern
engine database — one pass per body, whatever the rule count —
rebuilt whenever slots change. The engine scans literals, character
classes, alternation, groups, anchors, and small bounded repeats
(`\d{1,4}`, `[a-z]{2,10}`) exactly; those shapes carry no
tradeoff.

Patterns the engine cannot scan exactly — backreferences
(`(token)\1`), lookarounds (`(?=...)`, `(?!...)`, `(?<=...)`,
`(?<!...)`), word boundaries (`\b`, `\B`), possessive/atomic groups
— still ride the same pass as approximate prefilters: a cheap
approximation flags candidate bodies, and the true pattern confirms
only on those. The confirm is a full stdlib scan of that body for that
pattern — per-body cost returns, but bodies the approximation misses
pay nothing. An exact-shape rewrite (a quote-pair check as two
alternatives, a lookaround as a plain class) removes the confirm
outright; a rewrite that over-matches trades a bounded confirm for a
wrong finding, so the lookaround then stays.

Bounded repeats over wide content are the compile hazard: the engine
unrolls a bounded repeat into one state per step, so wide content
under large bounds multiplies compile time without limit —
`.{50,300}` compiles for seconds, `[^\n]{0,8000}` effectively
never. Over-budget shapes, and patterns that can match the empty
string, never reach the engine: they scan on the stdlib path behind
literal gates (a pattern's required literals must appear in the body
before its regex runs), which keeps unmatched bodies cheap.

Whichever path a pattern takes, findings are byte-identical — the
tradeoff is scan cost, never results. Response-header and request-side
matchers always run as direct stdlib matches (the texts are short);
only body patterns ride the engine.

One accepted tail: confirming and residual patterns run as plain
stdlib regex with no timeout or match budget, so a pathological
pattern (nested unbounded quantifiers over wide classes — the
catastrophic-backtracking shape) can stall the scan worker. The
session stays responsive; the drain waits. Keep patterns bounded and
literal-anchored.
