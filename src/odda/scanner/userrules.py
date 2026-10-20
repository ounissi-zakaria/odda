"""Rule slots: storage, loading, scopes, and live registration (ADR-0036).

Rules are Python files persisted as slot directories — one directory
per named slot holding one ``rule.py`` — using the proxy-script storage
convention (directory created lazily on first install, ``shutil.rmtree``
on remove). Slots live in two scopes: the project rules dir
(``<data_dir>/rules/<name>/``, what ``rule_install``/``rule_remove``
manage, per-project like every other data-dir subtree) and the global
rules dir under the odda config home (hand-managed, shared across every
project on the machine, ``ODDA_CONFIG_HOME``-redirectable). Both load
on session boot; a slot that fails to load is logged and skipped with
its error kept for ``rule_list``. A slot's file imports the rule-author
surface and exposes a module-level ``RULES`` list; one slot may
register any number of rules, and a slot may carry flat non-.py assets
(datasets) next to its rule file — installing from a file path copies
them along. No rules ship with odda: slots are the only rule source,
so the active ruleset is exactly the installed slots minus the disabled
rules.

Rule ids are permanent and unique across both scopes — they key the
append-only findings index — so install validates *before* persisting
and rejects a source whose ids collide with any installed slot's. A
disable is project state: an entry file in the data dir's scanner
subtree (presence = disabled) suppressing a whole slot by name or a
single rule by id. Disabled rules stay installed and listed; they are
withheld from the live registration, so disable and enable ride the
same arrival-time semantics as install and remove (ADR-0034): flows
already queued keep their epoch, findings already recorded are never
retracted.

The module stays inside the scanner's zero-egress boundary: stdlib
only, no flowstore/mitmproxy reach-ins — every directory arrives as a
parameter the same way the data dir reaches the Scanner itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from odda.scanner.model import Rule, Severity

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from odda.scanner.engine import Scanner

logger = logging.getLogger(__name__)

RULE_FILENAME = "rule.py"

#: Scope names as ``rule_list`` rows and error messages carry them.
#: Listing order is the tuple order (project first — the scope
#: install/remove act on); boot loads them the other way around so
#: global slots claim their ids first.
SCOPES = ("project", "global")


#: The rule-file contract, riding the sanctioned teaching surfaces: the
#: ``rule_install`` description and every load-failure rejection (a
#: format an agent cannot infer — it must be shown, not guessed).
RULE_FILE_SKELETON = """\
from odda.scanner import Rule, Severity

RULES = [
    Rule(
        id="example.marker",  # permanent, globally unique rule id
        severity=Severity.MEDIUM,  # info | low | medium | high | critical
        title="One-line title carried onto each finding",
        body_patterns=(r"regex-matched-against-the-stored-response-body",),
        # Other match kinds (each match becomes one evidence entry):
        # header_patterns — stored response header lines ("Name: value")
        # request_query_patterns — request URL query parameter names
        # request_header_patterns — stored request header lines
        # Optional gates — a flow must pass every gate that is set:
        # schemes=("http", "https"),  # request schemes only
        # statuses=(200, 404),  # response status codes only
        # content_types=("text/html",),  # content-type prefixes only
        # browser_only=False,  # True: browser-driven flows only
        # request_browser_only=False,  # True: request-side matchers see
        #   # browser-driven flows only
        # refs=("CWE-200",),  # carried onto findings
    ),
]
"""


def _rule_path(rules_dir: Path, name: str) -> Path:
    """Return the on-disk rule file path for the slot named ``name``."""
    return rules_dir / name / RULE_FILENAME


def install_source(rules_dir: Path, name: str, source: str) -> Path:
    """Persist ``source`` as slot ``name``'s rule file, overwriting.

    Creates the rules dir and the per-slot dir lazily. An existing
    slot dir is replaced wholesale, so assets carried by a previous
    file-install never linger under a swapped slot. This only writes
    the file; the live registration is the caller's step, so the boot
    scan and a force-overwrite reinstall share one write path.
    """
    path = _rule_path(rules_dir, name)
    if path.parent.exists():
        shutil.rmtree(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def install_assets(rules_dir: Path, name: str, source_file: Path | str) -> list[str]:
    """Copy ``source_file``'s sibling non-.py assets into the slot dir.

    A dataset-backed rule file reads its data from its own directory
    (the retire.js slot's ``jsrepository.json``), so installing from a
    file path must carry the file's flat non-Python siblings along.
    ``.py`` siblings and subdirectories stay behind: one slot is one
    rule file plus flat data assets. Returns the copied file names.
    """
    src = Path(source_file).resolve().parent
    dst = rules_dir / name
    copied: list[str] = []
    for entry in sorted(src.iterdir()):
        if not entry.is_file() or entry.suffix == ".py":
            continue
        shutil.copy2(entry, dst / entry.name)
        copied.append(entry.name)
    return copied


def remove_source(rules_dir: Path, name: str) -> None:
    """Delete slot ``name``'s directory.

    Raises:
        ValueError: If the slot is not on disk.
    """
    d = rules_dir / name
    if not d.exists():
        msg = f"Rule slot '{name}' not found"
        raise ValueError(msg)
    shutil.rmtree(d)


def list_slots(rules_dir: Path) -> list[dict[str, Any]]:
    """List persisted rule slots from disk, sorted by name."""
    if not rules_dir.exists():
        return []
    slots: list[dict[str, Any]] = []
    for entry in sorted(rules_dir.iterdir()):
        if not entry.is_dir():
            continue
        rule_file = entry / RULE_FILENAME
        if rule_file.exists():
            slots.append({"name": entry.name, "size": rule_file.stat().st_size})
    return slots


def name_exists(rules_dir: Path, name: str) -> bool:
    """Return True if slot ``name`` is persisted on disk."""
    return _rule_path(rules_dir, name).exists()


def load_slot_source(name: str, source: str, filename: str) -> list[Rule]:
    """Exec one rule file's source and return its ``RULES``.

    Runs under the same trust boundary as proxy-scripts: the source is
    arbitrary Python, executed for its module-level ``RULES`` list.
    The namespace is a real module, registered in ``sys.modules`` for
    the exec's duration: slot code is arbitrary Python, and stdlib
    machinery (dataclass field resolution above all) looks classes up
    through ``sys.modules[cls.__module__]`` — a bare dict namespace
    would make any slot defining a dataclass fail to load.

    Raises:
        ValueError: If the source fails to exec, exposes no ``RULES``
            list, or holds items that are not Rules with a valid
            severity. The message names the exact problem; the install
            surface appends the expected file shape.
    """
    modname = f"odda.rule-slots.{name}"
    module = ModuleType(modname, f"rule slot '{name}' exec namespace")
    module.__file__ = filename
    ns: dict[str, Any] = module.__dict__
    sys.modules[modname] = module
    try:
        exec(compile(source, filename, "exec"), ns)  # noqa: S102
    except Exception as exc:
        msg = f"Failed to load rule slot '{name}': {type(exc).__name__}: {exc}"
        raise ValueError(msg) from exc
    finally:
        del sys.modules[modname]
    if "RULES" not in ns:
        msg = f"Rule slot '{name}' defines no module-level RULES list"
        raise ValueError(msg)
    raw = ns["RULES"]
    if not isinstance(raw, (list, tuple)):
        msg = f"Rule slot '{name}' RULES must be a list of Rule objects"
        # ValueError (not TypeError): the message must ride the
        # ValueError-to-ToolError conversion the tool surface applies.
        raise ValueError(msg)  # noqa: TRY004
    rules = list(raw)
    if not rules:
        msg = f"Rule slot '{name}' defines an empty RULES list"
        raise ValueError(msg)
    for index, rule in enumerate(rules):
        if not isinstance(rule, Rule):
            msg = (
                f"Rule slot '{name}' RULES[{index}] is a "
                f"{type(rule).__name__}, not a Rule"
            )
            raise ValueError(msg)  # noqa: TRY004 — rides ToolError, see above
        if not isinstance(rule.severity, Severity):
            msg = (
                f"Rule slot '{name}' rule '{rule.id}' severity must be a "
                "Severity (info/low/medium/high/critical) — import Severity "
                "from odda.scanner and use e.g. Severity.MEDIUM"
            )
            raise ValueError(msg)  # noqa: TRY004 — rides ToolError, see above
    return rules


#: Quoted entry names at or under this length stay readable; longer
#: ones fall back to a hash (the raw value always rides the content).
_ENTRY_NAME_MAX = 120


def _entry_filename(kind: str, value: str) -> str:
    """Return the disable-entry filename for ``(kind, value)``.

    Injective by construction: ``quote`` escapes ``=``, so the quoted
    form never contains one, while the over-length fallback carries one
    right after the separator — the two forms cannot collide, and no
    two values can map to the same filename within a form.
    """
    quoted = quote(value, safe="")
    if len(quoted) <= _ENTRY_NAME_MAX:
        return f"{kind}--{quoted}"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{kind}--={digest}"


def _entry_path(disabled_dir: Path, kind: str, value: str) -> Path:
    """Return the on-disk path of one disable entry."""
    return disabled_dir / _entry_filename(kind, value)


def add_disabled_entry(disabled_dir: Path, kind: str, value: str) -> None:
    """Persist one disable entry; presence on disk means disabled.

    The entry dir appears lazily on the first entry (the data-dir
    precedent). The file lands via write-to-temp + rename, so a reader
    never observes a half-written entry, and sibling sessions racing on
    distinct entries touch distinct files — no lock, no lost updates.
    """
    path = _entry_path(disabled_dir, kind, value)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"kind": kind, "value": value}) + "\n"
    tmp = path.with_name(f"tmp-{path.name}")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def drop_disabled_entry(disabled_dir: Path, kind: str, value: str) -> None:
    """Delete one disable entry.

    Raises:
        FileNotFoundError: If the entry is not on disk (the caller
            checks the in-memory set first; a missing file means the
            two disagree and the louder failure is right).
    """
    _entry_path(disabled_dir, kind, value).unlink()


def load_disabled_entries(disabled_dir: Path) -> set[tuple[str, str]]:
    """Read every persisted disable entry as ``(kind, value)`` pairs.

    A corrupt entry file is logged and skipped — one bad file never
    blocks the rest, the failed-slot precedent.
    """
    entries: set[tuple[str, str]] = set()
    if not disabled_dir.exists():
        return entries
    for entry in sorted(disabled_dir.iterdir()):
        if not entry.is_file() or entry.name.startswith("tmp-"):
            continue
        try:
            doc = json.loads(entry.read_text(encoding="utf-8"))
            kind, value = doc["kind"], doc["value"]
        except (OSError, ValueError, KeyError, TypeError):
            # error, not exception: a corrupt entry is the expected,
            # recorded-and-skipped kind — no traceback noise
            logger.error(  # noqa: TRY400
                "Skipping corrupt disable entry '%s'", entry.name
            )
            continue
        if isinstance(kind, str) and isinstance(value, str):
            entries.add((kind, value))
        else:
            logger.error(
                "Skipping disable entry '%s': kind/value must be strings",
                entry.name,
            )
    return entries


@dataclass
class _SlotState:
    """What the manager knows about one slot.

    Either its live rules, or the load error that kept them out of the
    live set.
    """

    rules: tuple[Rule, ...] = ()
    error: str | None = None


def _row(
    slot: str | None,
    rule_id: str | None,
    severity: str | None,
    scope: str | None,
    state: str,
    error: str | None = None,
) -> dict[str, Any]:
    """One ``rule_list`` row — the single builder for every row kind."""
    return {
        "slot": slot,
        "id": rule_id,
        "severity": severity,
        "scope": scope,
        "state": state,
        "error": error,
    }


class RuleManager:
    """Manage rule slots across two scopes against a live Scanner.

    Project slots (the data dir's ``rules/`` tree) are what install
    and remove manage; global slots (the config home's rules dir) load
    alongside them but change only by hand. Disable entries suppress
    installed rules — a whole slot by name, a single rule by id —
    without uninstalling them. Every mutation (install, remove,
    disable, enable) recomputes the active ruleset — both scopes'
    loaded rules minus the covered ones — and applies the difference
    to the Scanner, so the engine's registration API and its
    arrival-time semantics stay untouched.

    All methods run on the event-loop thread (MCP tool handlers and
    the lifespan boot block); the scanner's drain never observes a
    half-registered slot. Mutations hold the manager's operation lock,
    and the epoch builds they trigger — which can cost seconds when a
    dataset corpus compiles — run off the loop in a worker thread; the
    scanner's own build lock serializes the builds.
    """

    def __init__(
        self, scanner: Scanner, global_dir: Path, project_dir: Path, disabled_dir: Path
    ) -> None:
        """Bind the scanner and the three rule-state directories.

        Args:
            scanner: The session's live scanner; rules register into
                it. It boots with an empty ruleset (ADR-0036) — slots
                are the only rule source.
            global_dir: The config home's rules dir — hand-managed,
                shared across every project on the machine.
            project_dir: The data dir's ``rules/`` subtree — what
                install/remove manage, appearing lazily on first
                install.
            disabled_dir: The data dir's ``scanner/disabled/``
                subtree, one file per disable entry, appearing lazily
                on the first disable.
        """
        self._scanner = scanner
        self._dirs = {"global": Path(global_dir), "project": Path(project_dir)}
        self._disabled_dir = Path(disabled_dir)
        self._slots: dict[str, dict[str, _SlotState]] = {scope: {} for scope in SCOPES}
        self._disabled: set[tuple[str, str]] = set()
        # What the last sync left registered in the scanner, id -> rule:
        # the diff baseline for the next mutation.
        self._active: dict[str, Rule] = {}
        # One manager operation at a time (install/remove/enable/
        # disable): the slot tables, the entry set, and the diff
        # baseline stay race-free while an operation's epoch build
        # runs off the loop.
        self._ops = asyncio.Lock()

    def name_exists(self, name: str) -> bool:
        """Return True if project slot ``name`` is persisted on disk.

        Install's name guard is project-scoped: names are per-tree, so
        a global slot may share the name, and the global tree is
        hand-managed — never touched here.
        """
        return name_exists(self._dirs["project"], name)

    async def install(
        self, name: str, source: str, *, assets_from: Path | str | None = None
    ) -> dict[str, Any]:
        """Validate, persist, and live-register project slot ``name``.

        Validation precedes the write (unlike proxy-scripts): a rule
        file that fails to load, or whose ids collide with any
        installed slot's — either scope — never reaches disk. Rules of
        the new slot that a disable entry already covers (the
        force-overwrite-a-disabled-slot path) register suppressed, so
        an update never silently re-enables what the project turned
        off.

        Args:
            name: The slot's name (its directory under the project
                rules dir).
            source: The rule file's Python source.
            assets_from: The file path the source was read from, when
                it arrived as a file: the file's sibling non-.py
                assets (datasets) are copied into the slot dir, so a
                dataset-backed rule rides along self-contained.
                Inline source installs carry no assets.

        Returns:
            ``{"name": ..., "size": N, "rules": [rule ids...],
            "assets": [copied file names...]}``.

        Raises:
            ValueError: If the source fails to load — the message then
                shows the expected file shape — or a rule id collides
                with another slot's in either scope (the message names
                the collision).
        """
        async with self._ops:
            path = _rule_path(self._dirs["project"], name)
            try:
                rules = load_slot_source(name, source, str(path))
            except ValueError as exc:
                msg = f"{exc}\nA rule file must look like:\n{RULE_FILE_SKELETON}"
                raise ValueError(msg) from None
            self._reject_id_collisions("project", name, rules)
            install_source(self._dirs["project"], name, source)
            assets = (
                install_assets(self._dirs["project"], name, assets_from)
                if assets_from
                else []
            )
            # The epoch build (dataset corpora compile pattern by
            # pattern) runs off the loop; the scanner's build lock
            # keeps concurrent registrations serialized.
            self._slots["project"][name] = _SlotState(rules=tuple(rules))
            await self._sync()
        return {
            "name": name,
            "size": path.stat().st_size,
            "rules": [rule.id for rule in rules],
            "assets": assets,
        }

    async def remove(self, name: str) -> dict[str, Any]:
        """Delete project slot ``name`` and its now-dangling disables.

        Past findings are never touched — the index is append-only;
        flows arriving after the remove produce nothing from these
        rules. Disable entries the remove empties go with the slot: a
        name entry no other slot of that name carries (either scope),
        and the id entries of this slot's own rules — so a fresh
        install of the same name starts active.

        Raises:
            ValueError: If the slot is not a project slot. A
                global-only name errors naming that scope and pointing
                at ``rule_disable``; the global tree is hand-managed.
        """
        async with self._ops:
            if not name_exists(self._dirs["project"], name):
                raise ValueError(self._remove_refusal(name))
            state = self._slots["project"].get(name)
            removed = [rule.id for rule in state.rules] if state else []
            remove_source(self._dirs["project"], name)
            self._slots["project"].pop(name, None)
            self._drop_dangling_entries(name, removed)
            await self._sync()
            return {"name": name, "removed": True, "rules": removed}

    async def disable(self, kind: str, value: str) -> dict[str, Any]:
        """Suppress an installed slot (``name``) or rule (``id``).

        The entry persists in the data dir and covers every loaded
        rule it names — a name entry covers every slot of that name in
        both scopes. Disabled rules stay installed and listed; they
        stop matching flows arriving after the disable. Strict: an
        existing entry, an unknown target, or a target already fully
        covered by another entry all error with nothing changed.

        Returns:
            ``{"disabled": True, "kind": ..., "value": ...,
            "rules": [ids that just went inactive]}``.

        Raises:
            ValueError: If the entry exists, the target is unknown, or
                the target is already disabled via another entry.
        """
        async with self._ops:
            self._reject_disable(kind, value)
            add_disabled_entry(self._disabled_dir, kind, value)
            self._disabled.add((kind, value))
            suppressed = [
                rule.id
                for scope, slot, rule in self._iter_rules()
                if rule.id in self._active
                and self._covered_by_entry(kind, value, slot, rule)
            ]
            await self._sync()
            return {
                "disabled": True,
                "kind": kind,
                "value": value,
                "rules": suppressed,
            }

    async def enable(self, kind: str, value: str) -> dict[str, Any]:
        """Remove one disable entry; the rules it covered come back.

        Enabling is arrival-time like every ruleset change: flows
        already queued stay suppressed, flows arriving after the
        enable match again. A rule still covered by another entry (a
        name entry alongside the removed id entry) stays disabled.

        Returns:
            ``{"enabled": True, "kind": ..., "value": ...,
            "rules": [ids that just went active]}``.

        Raises:
            ValueError: If no such entry exists — enabling a target
                that is not disabled is an error, not a no-op.
        """
        async with self._ops:
            if (kind, value) not in self._disabled:
                msg = f"{self._label(kind)} '{value}' is not disabled"
                raise ValueError(msg)
            self._take_entry(kind, value)
            restored = [
                rule.id
                for _, slot, rule in self._iter_rules()
                if rule.id not in self._active and not self._is_covered(slot, rule)
            ]
            await self._sync()
            return {"enabled": True, "kind": kind, "value": value, "rules": restored}

    def list_rules(self) -> list[dict[str, Any]]:
        """List every rule the session knows, across both scopes.

        One row per rule, project scope first then global (each sorted
        by slot name); a failed slot — one whose file would not load —
        contributes a single row carrying its load error instead of
        rules; a disable entry matching no installed rule renders as a
        target-missing row. With no slots and no entries there are no
        rows.
        """
        rows: list[dict[str, Any]] = []
        for scope in SCOPES:
            for name in sorted(self._slots[scope]):
                state = self._slots[scope][name]
                if state.error is not None:
                    rows.append(_row(name, None, None, scope, "failed", state.error))
                else:
                    rows.extend(
                        _row(
                            name,
                            rule.id,
                            rule.severity.value,
                            scope,
                            "disabled" if self._is_covered(name, rule) else "active",
                        )
                        for rule in state.rules
                    )
        rows.extend(self._orphan_rows())
        return rows

    def restore_on_boot(self) -> None:
        """Load both scopes' slots and the disable entries; register.

        Called during session boot, alongside the proxy-script restore
        and before observation goes live, so the session's very first
        flow is scanned against the full active ruleset. Global slots
        load first, then project: a project slot whose ids collide
        with a global slot's is logged and skipped with its error
        remembered (the install path would have rejected it). A slot
        that fails to load is skipped the same way — one bad slot
        never blocks the rest. The active set registers as one batch —
        one ruleset epoch, one compiled body database. No-op per dir
        that doesn't exist yet (lazy subtrees).
        """
        for scope in reversed(SCOPES):
            for slot in list_slots(self._dirs[scope]):
                name = slot["name"]
                rule_file = _rule_path(self._dirs[scope], name)
                try:
                    rules = load_slot_source(
                        name, rule_file.read_text(encoding="utf-8"), str(rule_file)
                    )
                    self._reject_id_collisions(scope, name, rules)
                except ValueError as exc:
                    self._slots[scope][name] = _SlotState(error=str(exc))
                    # error, not exception: the slot's failure is the
                    # expected, recorded-and-shown kind — no traceback
                    # noise
                    logger.error(  # noqa: TRY400
                        "Skipping %s rule slot '%s' on boot (load failed: %s)",
                        scope,
                        name,
                        exc,
                    )
                    continue
                self._slots[scope][name] = _SlotState(rules=tuple(rules))
                logger.info(
                    "Restored %s rule slot '%s' (%d rule(s))", scope, name, len(rules)
                )
        self._disabled = load_disabled_entries(self._disabled_dir)
        self._active = self._desired()
        if self._active:
            self._scanner.register_rules(list(self._active.values()))

    def _remove_refusal(self, name: str) -> str:
        """The remove error for a non-project slot, naming why."""
        if name_exists(self._dirs["global"], name):
            return (
                f"Rule slot '{name}' is global (the config home's rules "
                "dir); rule_remove only manages project slots — suppress "
                f"it in this project with rule_disable(name='{name}'), or "
                "delete it from the global rules dir by hand"
            )
        return f"Rule slot '{name}' not found"

    def _drop_dangling_entries(self, name: str, rule_ids: Sequence[str]) -> None:
        """Delete disable entries whose coverage a remove emptied.

        A name entry survives while any slot of that name remains
        (either scope); an id entry goes when its rule does — ids are
        unique across scopes, so the rule was this slot's.
        """
        if ("name", name) in self._disabled and not any(
            name in self._slots[scope] for scope in SCOPES
        ):
            self._take_entry("name", name)
        for rule_id in rule_ids:
            if ("id", rule_id) in self._disabled:
                self._take_entry("id", rule_id)

    def _take_entry(self, kind: str, value: str) -> None:
        """Delete one entry from disk and the in-memory set."""
        drop_disabled_entry(self._disabled_dir, kind, value)
        self._disabled.discard((kind, value))

    def _reject_disable(self, kind: str, value: str) -> None:
        """Strict disable validation: entry, target, then coverage."""
        if (kind, value) in self._disabled:
            msg = f"{self._label(kind)} '{value}' is already disabled"
            raise ValueError(msg)
        covered = self._covered_rules(kind, value)
        if not self._target_exists(kind, value):
            noun = "slot named" if kind == "name" else "with id"
            msg = (
                f"No rule {noun} '{value}' is installed in either scope — see rule_list"
            )
            raise ValueError(msg)
        if covered and all(rule.id not in self._active for _, _, rule in covered):
            # Covered, but every rule is already suppressed by another
            # entry — name the covering slot when one exists.
            via = next(
                (
                    other_value
                    for other_kind, other_value in self._disabled
                    if other_kind == "name" and other_value == covered[0][1]
                ),
                None,
            )
            msg = f"{self._label(kind)} '{value}' is already disabled"
            if via is not None:
                msg += f" (slot '{via}' is disabled by name)"
            raise ValueError(msg)

    def _covered_rules(self, kind: str, value: str) -> list[tuple[str, str, Rule]]:
        """Every loaded rule the entry ``(kind, value)`` covers."""
        return [
            (scope, slot, rule)
            for scope, slot, rule in self._iter_rules()
            if self._covered_by_entry(kind, value, slot, rule)
        ]

    def _target_exists(self, kind: str, value: str) -> bool:
        """Whether the disable target is installed (a failed slot is)."""
        if kind == "name":
            return any(value in self._slots[scope] for scope in SCOPES)
        return bool(self._covered_rules("id", value))

    def _orphan_rows(self) -> list[dict[str, Any]]:
        """Rows for disable entries matching no installed rule."""
        rows: list[dict[str, Any]] = []
        for kind, value in sorted(self._disabled):
            if self._covered_rules(kind, value) or self._target_exists(kind, value):
                continue
            rows.append(
                _row(
                    value if kind == "name" else None,
                    value if kind == "id" else None,
                    None,
                    None,
                    "disabled",
                    "disable entry matches no installed rule",
                )
            )
        return rows

    async def _sync(self) -> None:
        """Apply the active-set difference to the scanner, as epochs.

        The differential keeps a mid-session disable from recompiling
        the untouched global corpus: only what changed deregisters or
        registers, one build per non-empty side, off the loop.
        """
        desired = self._desired()
        dropped = frozenset(self._active) - desired.keys()
        added = [
            rule for rule_id, rule in desired.items() if rule_id not in self._active
        ]
        if dropped:
            await asyncio.to_thread(self._scanner.deregister_rules, dropped)
        if added:
            await asyncio.to_thread(self._scanner.register_rules, added)
        self._active = desired

    def _desired(self) -> dict[str, Rule]:
        """The active ruleset: both scopes' loaded rules, minus covered."""
        return {
            rule.id: rule
            for _, slot, rule in self._iter_rules()
            if not self._is_covered(slot, rule)
        }

    def _iter_rules(self) -> Iterator[tuple[str, str, Rule]]:
        """Every loaded rule as ``(scope, slot, rule)``, scopes sorted."""
        for scope in SCOPES:
            for slot, state in sorted(self._slots[scope].items()):
                if state.error is None:
                    for rule in state.rules:
                        yield scope, slot, rule

    def _is_covered(self, slot: str, rule: Rule) -> bool:
        """Whether a name entry on the slot or an id entry covers it."""
        return ("name", slot) in self._disabled or ("id", rule.id) in self._disabled

    def _covered_by_entry(self, kind: str, value: str, slot: str, rule: Rule) -> bool:
        """Whether this one entry covers this rule (sync helper)."""
        return value == (slot if kind == "name" else rule.id)

    @staticmethod
    def _label(kind: str) -> str:
        """The human noun for a disable kind, for error messages."""
        return "Rule slot" if kind == "name" else "Rule id"

    def _reject_id_collisions(
        self, scope: str, name: str, rules: Sequence[Rule]
    ) -> None:
        """Reject ids colliding across scopes or with a twin in scope.

        The slot's own current ids don't count within its own scope —
        that is the force-overwrite path, handled by replacing the
        slot's registration wholesale — but the other scope's slots do
        count: ids are unique across both scopes because they key the
        findings index alone.
        """
        seen: set[str] = set()
        for rule in rules:
            if rule.id in seen:
                msg = (
                    f"Rule id '{rule.id}' is defined twice in rule slot "
                    f"'{name}'; rule ids must be unique"
                )
                raise ValueError(msg)
            seen.add(rule.id)
            for other_scope, other_slot, other in self._iter_rules():
                if other_scope == scope and other_slot == name:
                    continue
                if other.id == rule.id:
                    msg = self._collision_message(rule.id, other_scope, other_slot)
                    raise ValueError(msg)

    @staticmethod
    def _collision_message(rule_id: str, scope: str, slot: str) -> str:
        """The id-collision rejection, naming where the id lives."""
        if scope == "project":
            return (
                f"Rule id '{rule_id}' is already registered by rule slot "
                f"'{slot}'; remove that slot or choose a different id"
            )
        return (
            f"Rule id '{rule_id}' is already registered by rule slot "
            f"'{slot}' in the global rules dir; rule ids are unique across "
            "both scopes — choose a different id"
        )
