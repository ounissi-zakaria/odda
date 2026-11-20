# See ADR-0036, ADR-0034.
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

SCOPES = ("project", "global")


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
    return rules_dir / name / RULE_FILENAME


def install_source(rules_dir: Path, name: str, source: str) -> Path:
    path = _rule_path(rules_dir, name)
    if path.parent.exists():
        shutil.rmtree(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def install_assets(rules_dir: Path, name: str, source_file: Path | str) -> list[str]:
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
    d = rules_dir / name
    if not d.exists():
        msg = f"Rule slot '{name}' not found"
        raise ValueError(msg)
    shutil.rmtree(d)


def list_slots(rules_dir: Path) -> list[dict[str, Any]]:
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
    return _rule_path(rules_dir, name).exists()


def load_slot_source(name: str, source: str, filename: str) -> list[Rule]:
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


_ENTRY_NAME_MAX = 120


def _entry_filename(kind: str, value: str) -> str:
    quoted = quote(value, safe="")
    if len(quoted) <= _ENTRY_NAME_MAX:
        return f"{kind}--{quoted}"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{kind}--={digest}"


def _entry_path(disabled_dir: Path, kind: str, value: str) -> Path:
    return disabled_dir / _entry_filename(kind, value)


def add_disabled_entry(disabled_dir: Path, kind: str, value: str) -> None:
    path = _entry_path(disabled_dir, kind, value)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"kind": kind, "value": value}) + "\n"
    tmp = path.with_name(f"tmp-{path.name}")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def drop_disabled_entry(disabled_dir: Path, kind: str, value: str) -> None:
    _entry_path(disabled_dir, kind, value).unlink()


def load_disabled_entries(disabled_dir: Path) -> set[tuple[str, str]]:
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
    return {
        "slot": slot,
        "id": rule_id,
        "severity": severity,
        "scope": scope,
        "state": state,
        "error": error,
    }


class RuleManager:
    def __init__(
        self, scanner: Scanner, global_dir: Path, project_dir: Path, disabled_dir: Path
    ) -> None:
        # See ADR-0036.
        self._scanner = scanner
        self._dirs = {"global": Path(global_dir), "project": Path(project_dir)}
        self._disabled_dir = Path(disabled_dir)
        self._slots: dict[str, dict[str, _SlotState]] = {scope: {} for scope in SCOPES}
        self._disabled: set[tuple[str, str]] = set()
        self._active: dict[str, Rule] = {}
        self._ops = asyncio.Lock()

    def name_exists(self, name: str, scope: str = "project") -> bool:
        return name_exists(self._dirs[scope], name)

    async def install(
        self,
        name: str,
        source: str,
        *,
        assets_from: Path | str | None = None,
        scope: str = "project",
    ) -> dict[str, Any]:
        async with self._ops:
            path = _rule_path(self._dirs[scope], name)
            try:
                rules = load_slot_source(name, source, str(path))
            except ValueError as exc:
                msg = f"{exc}\nA rule file must look like:\n{RULE_FILE_SKELETON}"
                raise ValueError(msg) from None
            self._reject_id_collisions(scope, name, rules)
            install_source(self._dirs[scope], name, source)
            assets = (
                install_assets(self._dirs[scope], name, assets_from)
                if assets_from
                else []
            )
            self._slots[scope][name] = _SlotState(rules=tuple(rules))
            await self._sync()
        return {
            "name": name,
            "scope": scope,
            "size": path.stat().st_size,
            "rules": [rule.id for rule in rules],
            "assets": assets,
        }

    async def remove(self, name: str, *, scope: str = "project") -> dict[str, Any]:
        async with self._ops:
            if not name_exists(self._dirs[scope], name):
                raise ValueError(self._remove_refusal(name, scope))
            state = self._slots[scope].get(name)
            removed = [rule.id for rule in state.rules] if state else []
            remove_source(self._dirs[scope], name)
            self._slots[scope].pop(name, None)
            self._drop_dangling_entries(name, removed)
            await self._sync()
            return {"name": name, "scope": scope, "removed": True, "rules": removed}

    async def disable(self, kind: str, value: str) -> dict[str, Any]:
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

    def _remove_refusal(self, name: str, scope: str) -> str:
        if scope == "project" and name_exists(self._dirs["global"], name):
            return (
                f"Rule slot '{name}' is global (the config home's rules "
                "dir); pass scope='global' to remove it, or suppress it "
                f"in this project with rule_disable(name='{name}')"
            )
        if scope == "global":
            return f"Rule slot '{name}' not found in the global rules dir"
        return f"Rule slot '{name}' not found"

    def _drop_dangling_entries(self, name: str, rule_ids: Sequence[str]) -> None:
        if ("name", name) in self._disabled and not any(
            name in self._slots[scope] for scope in SCOPES
        ):
            self._take_entry("name", name)
        for rule_id in rule_ids:
            if ("id", rule_id) in self._disabled:
                self._take_entry("id", rule_id)

    def _take_entry(self, kind: str, value: str) -> None:
        drop_disabled_entry(self._disabled_dir, kind, value)
        self._disabled.discard((kind, value))

    def _reject_disable(self, kind: str, value: str) -> None:
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
        return [
            (scope, slot, rule)
            for scope, slot, rule in self._iter_rules()
            if self._covered_by_entry(kind, value, slot, rule)
        ]

    def _target_exists(self, kind: str, value: str) -> bool:
        if kind == "name":
            return any(value in self._slots[scope] for scope in SCOPES)
        return bool(self._covered_rules("id", value))

    def _orphan_rows(self) -> list[dict[str, Any]]:
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
        return {
            rule.id: rule
            for _, slot, rule in self._iter_rules()
            if not self._is_covered(slot, rule)
        }

    def _iter_rules(self) -> Iterator[tuple[str, str, Rule]]:
        for scope in SCOPES:
            for slot, state in sorted(self._slots[scope].items()):
                if state.error is None:
                    for rule in state.rules:
                        yield scope, slot, rule

    def _is_covered(self, slot: str, rule: Rule) -> bool:
        return ("name", slot) in self._disabled or ("id", rule.id) in self._disabled

    def _covered_by_entry(self, kind: str, value: str, slot: str, rule: Rule) -> bool:
        return value == (slot if kind == "name" else rule.id)

    @staticmethod
    def _label(kind: str) -> str:
        return "Rule slot" if kind == "name" else "Rule id"

    def _reject_id_collisions(
        self, scope: str, name: str, rules: Sequence[Rule]
    ) -> None:
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
