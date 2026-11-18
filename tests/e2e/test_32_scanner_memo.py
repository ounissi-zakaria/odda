import asyncio
import time
from pathlib import Path

from odda.scanner.engine import _MEMO_BUDGET_BYTES, _MEMO_MAX_ENTRIES, Scanner
from odda.scanner.model import Evidence, Finding, FlowView, Rule, Severity
from tests.e2e.conftest import (
    FIXTURES,
    _findings_index,
    _seed_slot,
    _slot_rules,
    _stored_flow,
    curl,
    fixture_site,
)

_T14 = "t14-memo"
_FX = "stack-trace.html"


async def _drain_findings(
    scanner: Scanner,
    data_dir: Path,
    records: list[dict],
    expected: int | None = None,
    *,
    timeout: float = 120,
) -> tuple[list[dict], float]:
    drain = asyncio.create_task(scanner.run())
    try:
        findings_path = data_dir / "scanner" / "findings.jsonl"
        t0 = time.monotonic()
        for record in records:
            scanner.observe(record)
        settle_at = expected if expected is not None else len(records)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (
                findings_path.is_file()
                and findings_path.read_bytes().count(b"\n") >= settle_at
            ):
                break
            await asyncio.sleep(0.01)
        else:
            landed = (
                findings_path.read_bytes().count(b"\n")
                if findings_path.is_file()
                else 0
            )
            raise AssertionError(
                f"drain did not settle: {landed}/{settle_at} findings in {timeout}s"
            )
        elapsed = time.monotonic() - t0
        await asyncio.sleep(0.1)
    finally:
        drain.cancel()
        await asyncio.gather(drain, return_exceptions=True)
    return _findings_index(data_dir), elapsed


async def test_duplicate_body_burst_drains_in_small_fraction_of_unique_time(
    tmp_path,
) -> None:
    rules = (
        Rule(
            id="t14.burst",
            severity=Severity.LOW,
            title="Burst marker",
            body_patterns=(
                rf"{_T14}=[0-9]+",
                "never-there-literal-one",
                "absent-literal-two",
                r"window\._nothing",
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}",
            ),
        ),
        Rule(
            id="t14.burst-residual",
            severity=Severity.INFO,
            title="Burst residual",
            body_patterns=tuple(
                rf"({literal})-\1"
                for literal in (
                    "'payload",
                    "function",
                    "return",
                    "var",
                    "t14",
                    "abcdefghijklmnopqrstuvwxyz",
                    "0123456789",
                    "payload-abc",
                    "function f",
                    "return a",
                    "var a",
                    "f(){return",
                )
            ),
        ),
    )
    line = "var t14 = 'payload-abcdefghijklmnopqrstuvwxyz-0123456789';\n"
    filler = line * (1 * 1024 * 1024 // len(line))

    def burst(marker_value: int, nonce: int | None) -> bytes:
        tail = "" if nonce is None else f"// nonce {nonce}\n"
        return f"{_T14}={marker_value}\n{filler}{tail}".encode()

    async def drain_elapsed(data_dir: Path, bodies: list[bytes]) -> tuple[float, int]:
        records = [
            _stored_flow(data_dir, f"{i:05d}", body, query="t14param=yes")
            for i, body in enumerate(bodies)
        ]
        scanner = Scanner(data_dir, rules=rules)
        index, elapsed = await _drain_findings(scanner, data_dir, records, timeout=300)
        return elapsed, len(index)

    unique_dir = tmp_path / "unique"
    dup_dir = tmp_path / "duplicates"
    unique_elapsed, unique_findings = await drain_elapsed(
        unique_dir, [burst(1000 + i, i) for i in range(12)]
    )
    dup_elapsed, dup_findings = await drain_elapsed(dup_dir, [burst(7, None)] * 12)

    assert unique_findings == dup_findings == 12
    assert unique_elapsed > 0.4, (
        f"unique burst too fast to compare: {unique_elapsed:.3f}s"
    )
    assert dup_elapsed * 3 < unique_elapsed, (
        f"duplicate burst {dup_elapsed:.3f}s not a small fraction of "
        f"unique {unique_elapsed:.3f}s"
    )


async def test_body_memo_stays_bounded_under_pressure(tmp_path) -> None:
    rule = Rule(
        id="t14.pressure",
        severity=Severity.LOW,
        title="Pressure marker",
        body_patterns=(rf"{_T14}-[0-9]+",),
    )
    data_dir = tmp_path / ".odda"
    heavy = [(f"{_T14}-7\n" * 2500 + f"// nonce {i}\n").encode() for i in range(60)]
    tiny = [f"{_T14}-{1000 + i}\n".encode() for i in range(1100)]
    records = [
        _stored_flow(data_dir, f"{i:05d}", body, query="t14param=yes")
        for i, body in enumerate([*heavy, *tiny])
    ]
    scanner = Scanner(data_dir, rules=(rule,))
    index, _ = await _drain_findings(scanner, data_dir, records)
    memo = scanner._epoch.memo  # noqa: SLF001
    assert len(index) == len(records)
    assert len(memo) <= _MEMO_MAX_ENTRIES
    assert memo.bytes <= _MEMO_BUDGET_BYTES
    assert len(memo) < len(records), "no eviction under pressure"


async def test_duplicate_body_flows_each_record_their_own_findings(
    tmp_path,
) -> None:
    data_dir = tmp_path / ".odda"
    rules = (
        Rule(
            id="t14.body",
            severity=Severity.LOW,
            title="Memo body marker",
            body_patterns=(rf"{_T14}-[0-9]+",),
        ),
        Rule(
            id="t14.header",
            severity=Severity.INFO,
            title="Memo header marker",
            header_patterns=("X-T14-Hit",),
        ),
        Rule(
            id="t14.query",
            severity=Severity.INFO,
            title="Memo query marker",
            request_query_patterns=("t14param",),
        ),
    )
    body = f"leading text\n{_T14}-7 trailing text\n".encode()
    scanner = Scanner(data_dir, rules=rules)
    records = [
        _stored_flow(data_dir, flow_id, body, query="t14param=yes")
        for flow_id in ("00001", "00002")
    ]
    index, _ = await _drain_findings(scanner, data_dir, records)

    by_flow_rule = {(rec["flow_id"], rec["rule_id"]): rec for rec in index}
    assert set(by_flow_rule) == {
        (flow_id, rule_id)
        for flow_id in ("00001", "00002")
        for rule_id in ("t14.body", "t14.header", "t14.query")
    }
    for rule_id in ("t14.body", "t14.header", "t14.query"):
        first = dict(by_flow_rule[("00001", rule_id)])
        second = dict(by_flow_rule[("00002", rule_id)])
        assert first.pop("flow_id") == "00001"
        assert second.pop("flow_id") == "00002"
        assert first == second, rule_id


async def test_memoized_body_results_stay_pre_gate(tmp_path) -> None:
    rules = (
        Rule(
            id="t14.status-gated",
            severity=Severity.LOW,
            title="Status gated marker",
            body_patterns=(rf"{_T14}-[0-9]+",),
            statuses=(200,),
        ),
        Rule(
            id="t14.type-gated",
            severity=Severity.LOW,
            title="Type gated marker",
            body_patterns=(rf"{_T14}-[0-9]+",),
            content_types=("text/javascript",),
        ),
    )
    data_dir = tmp_path / ".odda"
    body = f"{_T14}-7 same bytes every flow\n".encode()
    specs = (
        ("00001", 500, "text/html"),
        ("00002", 200, "text/html"),
        ("00003", 200, "text/javascript"),
        ("00004", 500, "text/javascript"),
    )
    records = [
        _stored_flow(
            data_dir,
            flow_id,
            body,
            status_code=status,
            content_type=ctype,
            query="t14param=yes",
        )
        for flow_id, status, ctype in specs
    ]
    scanner = Scanner(data_dir, rules=rules)
    index, _ = await _drain_findings(scanner, data_dir, records)

    fired = {(rec["flow_id"], rec["rule_id"]) for rec in index}
    assert fired == {
        ("00002", "t14.status-gated"),
        ("00003", "t14.status-gated"),
        ("00003", "t14.type-gated"),
        ("00004", "t14.type-gated"),
    }
    assert all(rec["evidence"][0]["detail"] == f"{_T14}-7" for rec in index)


async def test_dataset_rule_gate_difference_stays_per_flow(tmp_path) -> None:
    rules = tuple(_slot_rules("retirejs"))
    body = (FIXTURES / "ticket05-jquery-old.js").read_bytes()
    data_dir = tmp_path / ".odda"
    records = [
        _stored_flow(
            data_dir,
            "00001",
            body,
            content_type="text/javascript",
            query="t14param=yes",
        ),
        _stored_flow(
            data_dir,
            "00002",
            body,
            content_type="text/plain",
            query="t14param=yes",
        ),
        _stored_flow(
            data_dir,
            "00003",
            body,
            content_type="text/javascript",
            query="t14param=yes",
        ),
    ]
    scanner = Scanner(data_dir, rules=rules)
    index, _ = await _drain_findings(scanner, data_dir, records, expected=2)

    fired = [rec["flow_id"] for rec in index if rec["rule_id"] == "js.outdated-library"]
    assert fired == ["00001", "00003"]
    first, third = [rec for rec in index if rec["flow_id"] in ("00001", "00003")]
    assert first["flow_id"] == "00001" and third["flow_id"] == "00003"
    assert all(rec["flow_id"] != "00002" for rec in index)
    first.pop("flow_id")
    third.pop("flow_id")
    assert first == third


async def test_scan_beyond_the_facet_never_replays_stale_findings(
    tmp_path,
) -> None:
    data_dir = tmp_path / ".odda"

    class _RequestHeaderRule(Rule):
        def scan(self, view: FlowView) -> list[Finding]:
            body = view.body
            hit = any(
                name.lower() == "x-t14-secret" for name, _ in view.request_headers
            )
            if body is None or not hit:
                return []
            return [
                Finding(
                    rule_id=self.id,
                    flow_id=str(view.record.get("id", "?")),
                    severity=self.severity,
                    title=self.title,
                    evidence=(
                        Evidence(where="request_headers", offset=0, detail="secret"),
                    ),
                )
            ]

    rule = _RequestHeaderRule(
        id="t14.beyond-facet",
        severity=Severity.LOW,
        title="Beyond the facet",
    )
    body = f"{_T14}-7 same bytes both flows\n".encode()
    records = [
        _stored_flow(data_dir, flow_id, body, query="t14param=yes")
        for flow_id in ("00001", "00002")
    ]
    (data_dir / "flows" / "00001" / "request").write_bytes(
        b"GET /00001.txt?t14param=yes HTTP/1.1\r\n"
        b"Host: fixture.local\r\nX-T14-Secret: yes\r\n\r\n"
    )
    scanner = Scanner(data_dir, rules=(rule,))
    index, _ = await _drain_findings(scanner, data_dir, records, expected=1)

    assert [rec["flow_id"] for rec in index] == ["00001"]
    assert index[0]["rule_id"] == "t14.beyond-facet"


async def test_rule_installed_after_body_seen_matches_its_rearrival(
    tmp_path,
) -> None:
    data_dir = tmp_path / ".odda"
    early = Rule(
        id="t14.seen-first",
        severity=Severity.LOW,
        title="Seen first",
        body_patterns=(rf"{_T14}-[0-9]+",),
    )
    body = f"{_T14}-7 body seen twice\n".encode()
    scanner = Scanner(data_dir, rules=(early,))
    scanner.observe(_stored_flow(data_dir, "00001", body, query="t14param=yes"))
    late = Rule(
        id="t14.installed-late",
        severity=Severity.INFO,
        title="Installed late",
        body_patterns=(rf"{_T14}-7",),
    )
    scanner.register_rules((late,))
    scanner.observe(_stored_flow(data_dir, "00002", body, query="t14param=yes"))
    findings_path = data_dir / "scanner" / "findings.jsonl"
    drain = asyncio.create_task(scanner.run())
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if findings_path.is_file() and findings_path.read_bytes().count(b"\n") >= 3:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    drain.cancel()
    await asyncio.gather(drain, return_exceptions=True)

    index = _findings_index(data_dir)
    assert sorted((rec["rule_id"], rec["flow_id"]) for rec in index) == [
        ("t14.installed-late", "00002"),
        ("t14.seen-first", "00001"),
        ("t14.seen-first", "00002"),
    ]


async def test_same_body_captured_twice_records_both_flows_findings(
    odda_session,
) -> None:
    async with odda_session() as h, fixture_site([_FX]) as fx:
        await _seed_slot(h, "disclosure")
        proxy = await h.call("proxy_url", {})
        flows = []
        for marker in ("t14-dup-one", "t14-dup-two"):
            r = await curl(
                f"{fx.base}/{_FX}?marker={marker}",
                proxy,
                extra=["-o", "/dev/null", "-w", "%{http_code}"],
            )
            assert r.stdout == "200"
            flows.append(await h.wait_flow(marker))
        first, second = flows

        first_finding = await h.wait_finding(first["id"])
        second_finding = await h.wait_finding(second["id"])

        assert first_finding["rule_id"] == "disclosure.stack-trace"
        assert second_finding["rule_id"] == "disclosure.stack-trace"
        assert first_finding["flow_id"] == first["id"]
        assert second_finding["flow_id"] == second["id"]
        first_finding.pop("flow_id")
        second_finding.pop("flow_id")
        assert first_finding == second_finding
