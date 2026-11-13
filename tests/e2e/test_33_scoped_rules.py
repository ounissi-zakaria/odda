"""Scoped rules end-to-end: two scopes, disables, strict semantics.

Project slots (``.odda/rules/``) and global slots (the config home's
rules dir, placed by hand in these tests the way a maintainer would)
load together; disables suppress without uninstalling. External
behavior only, the ``test_30`` habit: traffic in via curl through
proxy_url, findings out via a bounded poll of the index. Negative
assertions settle deterministically: the shared body carries a
Traceback banner, so a seeded disclosure finding on the same flow
proves the drain processed it before the absence is asserted.
"""

import asyncio
import shutil
from pathlib import Path

from tests.e2e.conftest import _seed_slot, curl, fixture_site

_T06_CANARY = "odda-ticket06-canary"
_T06_BODY = (
    "<html><body>ticket33 page; Traceback (most recent call last): "
    f'File "/app/t33.py"; canary {_T06_CANARY} present</body></html>\n'
)

_RULE_CANARY = f'''from odda.scanner import Rule, Severity

RULES = [
    Rule(
        id="user.ticket06-canary",
        severity=Severity.MEDIUM,
        title="Ticket06 canary marker disclosed",
        body_patterns=(r"{_T06_CANARY}",),
    ),
]
'''

_RULE_ALT = f'''from odda.scanner import Rule, Severity

RULES = [
    Rule(
        id="user.ticket06-alt",
        severity=Severity.LOW,
        title="Ticket06 alt marker disclosed",
        body_patterns=(r"{_T06_CANARY}",),
    ),
]
'''

# Two rules over the same body: a name disable must take the pair, an
# id disable exactly one of them.
_RULE_PAIR = f'''from odda.scanner import Rule, Severity

RULES = [
    Rule(
        id="user.t33-a",
        severity=Severity.LOW,
        title="Ticket33 pair member a",
        body_patterns=(r"{_T06_CANARY}",),
    ),
    Rule(
        id="user.t33-b",
        severity=Severity.LOW,
        title="Ticket33 pair member b",
        body_patterns=(r"{_T06_CANARY}",),
    ),
]
'''


def _place_global(config_home: Path, name: str, source: str) -> None:
    """Hand-place a global slot the maintainer's way: files in the
    config home's rules dir, no tool involved."""
    slot = config_home / "rules" / name
    slot.mkdir(parents=True, exist_ok=True)
    (slot / "rule.py").write_text(source, encoding="utf-8")


async def _drive(h, fx, marker: str) -> dict:
    proxy = await h.call("proxy_url", {})
    await curl(f"{fx.base}/?marker={marker}", proxy, extra=["-o", "/dev/null"])
    return await h.wait_flow(marker)


async def _wait_rule_finding(h, flow_id: str, rule_id: str) -> dict:
    """Bounded poll for one flow's finding from one specific rule."""
    for _ in range(200):
        for f in h.findings_for(flow_id):
            if f["rule_id"] == rule_id:
                return f
        await asyncio.sleep(0.05)
    raise AssertionError(f"no {rule_id!r} finding for flow {flow_id!r}")


async def _assert_no_finding(h, flow_id: str, rule_id: str) -> None:
    """Settle via the disclosure finding on the same flow (the drain is
    FIFO, one flow at a time), then assert the rule never matched."""
    await _wait_rule_finding(h, flow_id, "disclosure.stack-trace")
    assert all(f["rule_id"] != rule_id for f in h.findings_for(flow_id)), (
        f"unexpected {rule_id!r} finding for flow {flow_id!r}"
    )


async def _rows(h) -> dict[tuple[str, str], dict]:
    return {
        (row["scope"], row["id"]): row for row in await h.call("rule_list", {})
    }


async def test_disable_by_name_suppresses_future_only_and_enable_restores(
    odda_session,
) -> None:
    """A name disable takes the slot's rules out of the live set from
    arrival: findings already recorded stay, later flows never match,
    the row stays listed as disabled, and enable restores matching."""
    async with odda_session() as h, fixture_site(index_body=_T06_BODY) as fx:
        await _seed_slot(h, "disclosure")
        await h.call("rule_install", {"name": "canary", "source": _RULE_CANARY})
        before = await _drive(h, fx, "t33-before")
        await _wait_rule_finding(h, before["id"], "user.ticket06-canary")

        r = await h.call("rule_disable", {"name": "canary"})
        assert r == {
            "disabled": True,
            "kind": "name",
            "value": "canary",
            "rules": ["user.ticket06-canary"],
        }

        after = await _drive(h, fx, "t33-after")
        await _assert_no_finding(h, after["id"], "user.ticket06-canary")
        assert any(
            f["rule_id"] == "user.ticket06-canary" for f in h.findings_for(before["id"])
        )

        row = (await _rows(h))[("project", "user.ticket06-canary")]
        assert row["state"] == "disabled"

        r = await h.call("rule_enable", {"name": "canary"})
        assert r == {
            "enabled": True,
            "kind": "name",
            "value": "canary",
            "rules": ["user.ticket06-canary"],
        }
        back = await _drive(h, fx, "t33-back")
        await _wait_rule_finding(h, back["id"], "user.ticket06-canary")


async def test_disable_by_id_takes_one_rule_of_a_slot(odda_session) -> None:
    """An id disable suppresses exactly the named rule; the sibling in
    the same slot keeps firing, and the listing shows the split."""
    async with odda_session() as h, fixture_site(index_body=_T06_BODY) as fx:
        await _seed_slot(h, "disclosure")
        await h.call("rule_install", {"name": "pair", "source": _RULE_PAIR})

        r = await h.call("rule_disable", {"id": "user.t33-a"})
        assert r["rules"] == ["user.t33-a"]

        flow = await _drive(h, fx, "t33-pair")
        await _wait_rule_finding(h, flow["id"], "user.t33-b")
        await _assert_no_finding(h, flow["id"], "user.t33-a")

        rows = await _rows(h)
        assert rows[("project", "user.t33-a")]["state"] == "disabled"
        assert rows[("project", "user.t33-b")]["state"] == "active"

        r = await h.call("rule_enable", {"id": "user.t33-a"})
        assert r["rules"] == ["user.t33-a"]
        again = await _drive(h, fx, "t33-pair-back")
        await _wait_rule_finding(h, again["id"], "user.t33-a")


async def test_disable_and_enable_are_strict_everywhere(odda_session) -> None:
    """The XOR param contract, unknown targets, double disables, an id
    under a name-disabled slot, and enabling with the wrong kind all
    error verbatim with nothing changed."""
    async with odda_session() as h:
        await h.call("rule_install", {"name": "canary", "source": _RULE_CANARY})

        assert await h.call_error("rule_disable", {}) == (
            "Pass exactly one of name or id"
        )
        assert await h.call_error(
            "rule_disable", {"name": "canary", "id": "user.ticket06-canary"}
        ) == ("Pass exactly one of name or id")

        err = await h.call_error("rule_disable", {"name": "ghost"})
        assert "No rule slot named 'ghost' is installed in either scope" in err
        err = await h.call_error("rule_disable", {"id": "user.ghost"})
        assert "No rule with id 'user.ghost' is installed in either scope" in err

        await h.call("rule_disable", {"name": "canary"})
        err = await h.call_error("rule_disable", {"name": "canary"})
        assert "Rule slot 'canary' is already disabled" in err
        err = await h.call_error("rule_disable", {"id": "user.ticket06-canary"})
        assert err == (
            "Rule id 'user.ticket06-canary' is already disabled "
            "(slot 'canary' is disabled by name)"
        )
        err = await h.call_error("rule_enable", {"id": "user.ticket06-canary"})
        assert err == "Rule id 'user.ticket06-canary' is not disabled"

        await h.call("rule_enable", {"name": "canary"})
        err = await h.call_error("rule_enable", {"name": "canary"})
        assert err == "Rule slot 'canary' is not disabled"


async def test_removing_a_global_name_errors_and_disable_covers_globals(
    odda_session, config_home
) -> None:
    """rule_remove refuses a global-only name, saying it is global and
    naming rule_disable; the global tree stays untouched, and disabling
    the global slot in this project stops its future findings."""
    _place_global(config_home, "curated", _RULE_ALT)
    async with odda_session() as h, fixture_site(index_body=_T06_BODY) as fx:
        await _seed_slot(h, "disclosure")
        err = await h.call_error("rule_remove", {"name": "curated"})
        assert "Rule slot 'curated' is global" in err
        assert "rule_disable(name='curated')" in err
        assert (config_home / "rules" / "curated" / "rule.py").is_file()

        before = await _drive(h, fx, "t33-global-before")
        await _wait_rule_finding(h, before["id"], "user.ticket06-alt")

        r = await h.call("rule_disable", {"name": "curated"})
        assert r["rules"] == ["user.ticket06-alt"]
        after = await _drive(h, fx, "t33-global-after")
        await _assert_no_finding(h, after["id"], "user.ticket06-alt")

        row = (await _rows(h))[("global", "user.ticket06-alt")]
        assert row["slot"] == "curated"
        assert row["state"] == "disabled"


async def test_remove_clears_its_disable_entries_reinstall_starts_active(
    odda_session,
) -> None:
    """Removing a project slot takes its disable entries with it — the
    entry dir empties, and a fresh install of the same name starts
    active rather than silently disabled."""
    async with odda_session() as h, fixture_site(index_body=_T06_BODY) as fx:
        await _seed_slot(h, "disclosure")
        await h.call("rule_install", {"name": "byname", "source": _RULE_CANARY})
        await h.call("rule_install", {"name": "byid", "source": _RULE_ALT})
        await h.call("rule_disable", {"name": "byname"})
        await h.call("rule_disable", {"id": "user.ticket06-alt"})
        disabled_dir = h.data_dir / "scanner" / "disabled"
        assert any(disabled_dir.iterdir())

        await h.call("rule_remove", {"name": "byname"})
        await h.call("rule_remove", {"name": "byid"})
        assert not any(disabled_dir.iterdir())

        await h.call("rule_install", {"name": "byname", "source": _RULE_CANARY})
        rows = await _rows(h)
        assert rows[("project", "user.ticket06-canary")]["state"] == "active"
        flow = await _drive(h, fx, "t33-reinstall")
        await _wait_rule_finding(h, flow["id"], "user.ticket06-canary")


async def test_force_reinstall_keeps_the_name_disable(odda_session) -> None:
    """A force overwrite keeps a slot's name-keyed disable across the
    swap: the swapped-in rule stays suppressed, and enabling the name
    lets it fire."""
    async with odda_session() as h, fixture_site(index_body=_T06_BODY) as fx:
        await _seed_slot(h, "disclosure")
        await h.call("rule_install", {"name": "swap", "source": _RULE_CANARY})
        await h.call("rule_disable", {"name": "swap"})
        await h.call(
            "rule_install", {"name": "swap", "source": _RULE_ALT, "force": True}
        )

        assert (await _rows(h))[("project", "user.ticket06-alt")][
            "state"
        ] == "disabled"
        flow = await _drive(h, fx, "t33-force")
        await _assert_no_finding(h, flow["id"], "user.ticket06-alt")

        await h.call("rule_enable", {"name": "swap"})
        back = await _drive(h, fx, "t33-force-back")
        await _wait_rule_finding(h, back["id"], "user.ticket06-alt")


async def test_disables_persist_across_boots(odda_session) -> None:
    """A disable is data-dir state: the next session's boot restores
    it (the rule stays suppressed from the very first flow) and the
    entry stays enableable."""
    async with odda_session() as h:
        await h.call("rule_install", {"name": "canary", "source": _RULE_CANARY})
        await h.call("rule_disable", {"id": "user.ticket06-canary"})

    async with odda_session() as h, fixture_site(index_body=_T06_BODY) as fx:
        await _seed_slot(h, "disclosure")
        assert (await _rows(h))[("project", "user.ticket06-canary")][
            "state"
        ] == "disabled"
        flow = await _drive(h, fx, "t33-boot")
        await _assert_no_finding(h, flow["id"], "user.ticket06-canary")

        await h.call("rule_enable", {"id": "user.ticket06-canary"})
        back = await _drive(h, fx, "t33-boot-back")
        await _wait_rule_finding(h, back["id"], "user.ticket06-canary")


async def test_stale_disable_entry_renders_target_missing_and_clears(
    odda_session, config_home
) -> None:
    """An entry whose target vanished out-of-band (the global slot was
    hand-removed) renders as a target-missing row — visible, not silent
    — and rule_enable clears it."""
    _place_global(config_home, "gone", _RULE_CANARY)
    async with odda_session() as h:
        await h.call("rule_disable", {"name": "gone"})

    shutil.rmtree(config_home / "rules" / "gone")
    async with odda_session() as h:
        assert await h.call("rule_list", {}) == [
            {
                "slot": "gone",
                "id": None,
                "severity": None,
                "scope": None,
                "state": "disabled",
                "error": "disable entry matches no installed rule",
            }
        ]
        r = await h.call("rule_enable", {"name": "gone"})
        assert r == {"enabled": True, "kind": "name", "value": "gone", "rules": []}
        assert await h.call("rule_list", {}) == []


async def test_cross_scope_id_collision_rejected_at_install(
    odda_session, config_home
) -> None:
    """Ids are unique across both scopes: an install whose id a global
    slot already holds is rejected naming the global slot, and nothing
    reaches the project tree."""
    _place_global(config_home, "curated", _RULE_ALT)
    async with odda_session() as h:
        err = await h.call_error("rule_install", {"name": "clash", "source": _RULE_ALT})
        assert (
            "Rule id 'user.ticket06-alt' is already registered by rule slot "
            "'curated' in the global rules dir" in err
        )
        assert "unique across both scopes" in err
        assert not (h.data_dir / "rules" / "clash").exists()


async def test_name_disable_covers_both_scopes_twin_names(
    odda_session, config_home
) -> None:
    """A slot name may exist in both trees, and a name disable covers
    every slot of that name — both scopes' rows go disabled, neither
    fires, and enable brings both back."""
    _place_global(config_home, "twin", _RULE_ALT)
    async with odda_session() as h, fixture_site(index_body=_T06_BODY) as fx:
        await _seed_slot(h, "disclosure")
        await h.call("rule_install", {"name": "twin", "source": _RULE_CANARY})

        r = await h.call("rule_disable", {"name": "twin"})
        assert r["rules"] == ["user.ticket06-canary", "user.ticket06-alt"]

        rows = await _rows(h)
        assert rows[("project", "user.ticket06-canary")]["state"] == "disabled"
        assert rows[("global", "user.ticket06-alt")]["state"] == "disabled"

        flow = await _drive(h, fx, "t33-twin")
        await _assert_no_finding(h, flow["id"], "user.ticket06-canary")
        await _assert_no_finding(h, flow["id"], "user.ticket06-alt")

        await h.call("rule_enable", {"name": "twin"})
        back = await _drive(h, fx, "t33-twin-back")
        await _wait_rule_finding(h, back["id"], "user.ticket06-canary")
        await _wait_rule_finding(h, back["id"], "user.ticket06-alt")
