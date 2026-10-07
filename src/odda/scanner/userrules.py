"""User rule slots: storage, loading, and live registration (ADR-0035).

User rules are Python files persisted as slot directories under
``<data_dir>/rules/<name>/rule.py`` — the proxy-script storage
convention (one Python file per named slot, directory created lazily
on first install, ``shutil.rmtree`` on remove). A slot's file imports
the rule-author surface and exposes a module-level ``RULES`` list; one
slot may register any number of rules. :class:`UserRuleManager` loads
slots into a live Scanner at install time and again on session boot;
a slot that fails to load is logged and skipped (one bad slot never
blocks the rest), with its error kept for ``rule_list`` to show.

Two deliberate divergences from the proxy-script flow:

- Install validates *before* persisting. Rule ids are permanent and
  global — they key the append-only findings index — so a source that
  fails to load, or whose ids collide with a builtin's or another
  slot's, must never reach disk. (A rejected proxy-script is left on
  disk so boot retries it; a rejected rule slot would instead poison
  every future boot's id space.)
- The engine snapshots the live rules at each flow's *arrival*, so an
  installed rule applies to flows arriving after the install only —
  never retroactively to backlog still queued for scanning.

The module stays inside the scanner's zero-egress boundary: stdlib
only, no flowstore/mitmproxy reach-ins — the data dir arrives as a
parameter the same way it reaches the Scanner itself.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from odda.scanner.model import Rule, Severity

if TYPE_CHECKING:
    from collections.abc import Sequence

    from odda.scanner.engine import Scanner

logger = logging.getLogger(__name__)

RULES_DIR_NAME = "rules"
RULE_FILENAME = "rule.py"

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


def _rules_dir(data_dir: Path) -> Path:
    """Return the rules dir under ``data_dir`` (not created here)."""
    return data_dir / RULES_DIR_NAME


def _rule_path(data_dir: Path, name: str) -> Path:
    """Return the on-disk rule file path for the slot named ``name``."""
    return _rules_dir(data_dir) / name / RULE_FILENAME


def install_source(data_dir: Path, name: str, source: str) -> Path:
    """Persist ``source`` as slot ``name``'s rule file, overwriting.

    Creates the rules dir and the per-slot dir lazily. This only writes
    the file; the live registration is the caller's step, so the boot
    scan and a force-overwrite reinstall share one write path.
    """
    path = _rule_path(data_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def remove_source(data_dir: Path, name: str) -> None:
    """Delete slot ``name``'s directory.

    Raises:
        ValueError: If the slot is not on disk.
    """
    d = _rules_dir(data_dir) / name
    if not d.exists():
        msg = f"Rule slot '{name}' not found"
        raise ValueError(msg)
    shutil.rmtree(d)


def list_slots(data_dir: Path) -> list[dict[str, Any]]:
    """List persisted rule slots from disk, sorted by name."""
    d = _rules_dir(data_dir)
    if not d.exists():
        return []
    slots: list[dict[str, Any]] = []
    for entry in sorted(d.iterdir()):
        if not entry.is_dir():
            continue
        rule_file = entry / RULE_FILENAME
        if rule_file.exists():
            slots.append({"name": entry.name, "size": rule_file.stat().st_size})
    return slots


def name_exists(data_dir: Path, name: str) -> bool:
    """Return True if slot ``name`` is persisted on disk."""
    return _rule_path(data_dir, name).exists()


def load_slot_source(name: str, source: str, filename: str) -> list[Rule]:
    """Exec one rule file's source and return its ``RULES``.

    Runs under the same trust boundary as proxy-scripts: the source is
    arbitrary Python, executed for its module-level ``RULES`` list.

    Raises:
        ValueError: If the source fails to exec, exposes no ``RULES``
            list, or holds items that are not Rules with a valid
            severity. The message names the exact problem; the install
            surface appends the expected file shape.
    """
    ns: dict[str, Any] = {"__name__": f"odda.rule-slots.{name}", "__file__": filename}
    try:
        exec(compile(source, filename, "exec"), ns)  # noqa: S102
    except Exception as exc:
        msg = f"Failed to load rule slot '{name}': {type(exc).__name__}: {exc}"
        raise ValueError(msg) from exc
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


@dataclass
class _SlotState:
    """What the manager knows about one slot.

    Either its live rules, or the load error that kept them out of the
    live set.
    """

    rules: tuple[Rule, ...] = ()
    error: str | None = None


def _row(rule: Rule, slot: str | None, source: str) -> dict[str, Any]:
    """One ``rule_list`` row; keys stay uniform across row kinds."""
    return {
        "slot": slot,
        "id": rule.id,
        "severity": rule.severity.value,
        "source": source,
        "state": "active",
        "error": None,
    }


class UserRuleManager:
    """Manages user rule slots against a live Scanner.

    Mirrors the proxy-script manager's shape: install persists and
    registers live, remove deregisters and deletes, and the boot scan
    reloads every persisted slot with per-slot failure isolation. All
    methods run on the event-loop thread (MCP tool handlers and the
    lifespan boot block); the scanner's drain never observes a
    half-registered slot.
    """

    def __init__(
        self, scanner: Scanner, data_dir: Path, builtins: Sequence[Rule]
    ) -> None:
        """Bind the scanner, its data dir, and the builtin ruleset.

        Args:
            scanner: The session's live scanner; slots register into it.
            data_dir: The session's ``.odda`` directory; the rules
                subtree appears lazily under it on the first install.
            builtins: The builtin rules the scanner was constructed
                with — their ids are permanently taken, so user rules
                may never collide with one.
        """
        self._scanner = scanner
        self._data_dir = Path(data_dir)
        self._builtins: tuple[Rule, ...] = tuple(builtins)
        self._builtin_ids = frozenset(rule.id for rule in self._builtins)
        self._slots: dict[str, _SlotState] = {}

    def name_exists(self, name: str) -> bool:
        """Return True if slot ``name`` is persisted on disk."""
        return name_exists(self._data_dir, name)

    def install(self, name: str, source: str) -> dict[str, Any]:
        """Validate, persist, and live-register slot ``name``.

        Validation precedes the write (unlike proxy-scripts): a rule
        file that fails to load or collides on id never reaches disk.
        Overwriting an existing slot (the ``force`` path, gated by the
        caller) deregisters its old rules first, so a slot's ids are
        always exactly the file's.

        Returns:
            ``{"name": ..., "size": N, "rules": [rule ids...]}``.

        Raises:
            ValueError: If the source fails to load — the message then
                shows the expected file shape — or a rule id collides
                with a builtin's or another slot's (the message names
                the collision).
        """
        path = _rule_path(self._data_dir, name)
        try:
            rules = load_slot_source(name, source, str(path))
        except ValueError as exc:
            msg = f"{exc}\nA rule file must look like:\n{RULE_FILE_SKELETON}"
            raise ValueError(msg) from None
        self._reject_id_collisions(name, rules)
        self._deregister(name)
        install_source(self._data_dir, name, source)
        self._scanner.register_rules(rules)
        self._slots[name] = _SlotState(rules=tuple(rules))
        return {
            "name": name,
            "size": path.stat().st_size,
            "rules": [rule.id for rule in rules],
        }

    def remove(self, name: str) -> dict[str, Any]:
        """Deregister slot ``name``'s rules live and delete the slot.

        Past findings are never touched — the index is append-only;
        flows arriving after the remove produce nothing from these
        rules.

        Raises:
            ValueError: If the slot is not on disk.
        """
        removed = self._deregister(name)
        remove_source(self._data_dir, name)
        return {"name": name, "removed": True, "rules": removed}

    def list_rules(self) -> list[dict[str, Any]]:
        """List every rule the session knows.

        Builtin rows come first (registration order), then one row per
        user rule grouped by slot name; a failed slot — one whose file
        would not load at boot — contributes a single row carrying its
        load error instead of rules.
        """
        rows = [_row(rule, None, "builtin") for rule in self._builtins]
        for name in sorted(self._slots):
            state = self._slots[name]
            if state.error is not None:
                rows.append(
                    {
                        "slot": name,
                        "id": None,
                        "severity": None,
                        "source": "user",
                        "state": "failed",
                        "error": state.error,
                    }
                )
            else:
                rows.extend(_row(rule, name, "user") for rule in state.rules)
        return rows

    def restore_on_boot(self) -> None:
        """Load every persisted slot into the live scanner.

        Called during session boot, alongside the proxy-script restore
        and before observation goes live, so the session's very first
        flow is scanned against the full ruleset. A slot that fails to
        load (or collides on id) is logged and skipped with its error
        remembered for ``rule_list``; the rest still come up. No-op
        when the rules dir doesn't exist yet (lazy data dir).
        """
        for slot in list_slots(self._data_dir):
            name = slot["name"]
            rule_file = _rule_path(self._data_dir, name)
            try:
                rules = load_slot_source(
                    name, rule_file.read_text(encoding="utf-8"), str(rule_file)
                )
                self._reject_id_collisions(name, rules)
            except ValueError as exc:
                self._slots[name] = _SlotState(error=str(exc))
                # error, not exception: the slot's failure is the
                # expected, recorded-and-shown kind — no traceback noise
                logger.error(  # noqa: TRY400
                    "Skipping rule slot '%s' on boot (load failed: %s)", name, exc
                )
                continue
            self._scanner.register_rules(rules)
            self._slots[name] = _SlotState(rules=tuple(rules))
            logger.info("Restored rule slot '%s' (%d rule(s))", name, len(rules))

    def _deregister(self, name: str) -> list[str]:
        """Drop slot ``name``'s live registration; return its rule ids."""
        state = self._slots.pop(name, None)
        if state is None or not state.rules:
            return []
        ids = [rule.id for rule in state.rules]
        self._scanner.deregister_rules(frozenset(ids))
        return ids

    def _reject_id_collisions(self, name: str, rules: Sequence[Rule]) -> None:
        """Reject ids colliding with builtins, other slots, or a twin.

        The slot's own current ids don't count — that is the force-
        overwrite path, handled by deregistering before re-registering.
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
            if rule.id in self._builtin_ids:
                msg = (
                    f"Rule id '{rule.id}' is already a builtin rule id; rule "
                    "ids are permanent and global — choose a different id"
                )
                raise ValueError(msg)
            for slot, state in self._slots.items():
                if slot == name:
                    continue
                if any(r.id == rule.id for r in state.rules):
                    msg = (
                        f"Rule id '{rule.id}' is already registered by rule "
                        f"slot '{slot}'; remove that slot or choose a "
                        "different id"
                    )
                    raise ValueError(msg)
