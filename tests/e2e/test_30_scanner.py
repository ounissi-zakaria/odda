"""Scanner tracer bullet: the first Finding lands end-to-end.

A session-live passive scanner observes every flow the session writes
— the flow record writer's post-append hook enqueues, an asyncio drain
task owned by the lifespan scans — and appends findings to
``<data_dir>/scanner/findings.jsonl`` (one line per rule-flow pair,
evidence aggregated). One builtin rule exists:
``disclosure.stack-trace`` (HIGH). External behavior only: traffic in
via curl through proxy_url or request_send, findings out via a bounded
poll of the index (the ``wait_flow`` precedent).
"""

import ast
import asyncio
import json
from pathlib import Path

from tests.e2e.conftest import curl, fixture_site

_FX = "stack-trace.html"


async def test_stack_trace_response_lands_finding(odda_session) -> None:
    """A stack-trace body driven through the proxy lands one Finding with
    rule id, flow id, severity high, title, located evidence, and refs."""
    async with odda_session() as h, fixture_site([_FX]) as fx:
        proxy = await h.call("proxy_url", {})
        marker = "scan-tracer"
        r = await curl(
            f"{fx.base}/{_FX}?marker={marker}",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"

        flow = await h.wait_flow(marker)
        finding = await h.wait_finding(flow["id"])

        assert finding["rule_id"] == "disclosure.stack-trace"
        assert finding["flow_id"] == flow["id"]
        assert finding["severity"] == "high"
        assert finding["title"]
        assert "CWE-209" in finding["refs"]
        first = finding["evidence"][0]
        assert first["where"] == "response_body"
        assert isinstance(first["offset"], int)
        assert "Traceback" in first["detail"]


async def test_multi_match_aggregates_into_one_finding(odda_session) -> None:
    """Multiple trace matches inside one flow aggregate into that single
    Finding's evidence — one Finding per rule-flow pair."""
    async with odda_session() as h, fixture_site([_FX]) as fx:
        proxy = await h.call("proxy_url", {})
        marker = "scan-aggregate"
        r = await curl(
            f"{fx.base}/{_FX}?marker={marker}",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"

        flow = await h.wait_flow(marker)
        finding = await h.wait_finding(flow["id"])

        # The fixture's match inventory: one Python traceback, two Java
        # frames, a PHP fatal plus two PHP frames, two Node frames, one
        # .NET frame — nine matches, one finding.
        assert len(finding["evidence"]) == 9
        assert len({e["offset"] for e in finding["evidence"]}) == 9
        assert len(h.findings_for(flow["id"])) == 1


async def test_request_send_flow_is_scanned(odda_session) -> None:
    """The observation hook covers the request sender's write path too —
    request_send records flow on the event-loop thread, browser capture
    on the mitmproxy addon thread."""
    async with odda_session() as h, fixture_site([_FX]) as fx:
        await h.call(
            "request_new",
            {
                "name": "scan-reqsend",
                "host": "127.0.0.1",
                "protocol": "http",
                "port": fx.port,
            },
        )
        h.write_request(
            "scan-reqsend",
            (
                f"GET {fx.path}/{_FX} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{fx.port}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode(),
        )
        rec = await h.call_json("request_send", {"name": "scan-reqsend", "timeout": 10})
        assert isinstance(rec, dict)

        finding = await h.wait_finding(rec["id"])
        assert finding["rule_id"] == "disclosure.stack-trace"
        assert finding["flow_id"] == rec["id"]


async def test_clean_and_error_flows_produce_no_finding(odda_session) -> None:
    """A clean response and an error flow (no response side) produce no
    Finding, and the scanner's data-dir subtree appears lazily — absent
    until the first Finding."""
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        proxy = await h.call("proxy_url", {})
        r = await curl(
            f"{fx.base}/?marker=scan-clean",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"
        clean = await h.wait_flow("scan-clean")
        assert clean["error"] is None

        # A flow with no response at all (closed port): recorded as an
        # error flow, nothing for a response-side rule to match.
        await curl(
            "http://127.0.0.1:9/?marker=scan-err",
            proxy,
            extra=["-o", "/dev/null"],
        )
        err = await h.wait_flow("scan-err")
        assert err["status_code"] is None
        assert err["error"]

        # Both flows were drained (they precede nothing else, but give
        # the drain a generous bounded settle before the negative read).
        await asyncio.sleep(0.5)
        assert not (h.data_dir / "scanner").exists(), (
            "scanner dir must appear lazily on the first finding"
        )


async def test_scan_failure_never_touches_capture(odda_session) -> None:
    """A finding that cannot be recorded — the scanner dir is blocked by
    a plain file — is logged and skipped; the flow still lands fully in
    the flows index and the session keeps capturing."""
    async with odda_session() as h, fixture_site([_FX]) as fx:
        blocked = h.data_dir / "scanner"
        blocked.parent.mkdir(parents=True, exist_ok=True)
        blocked.write_text("not a directory")

        proxy = await h.call("proxy_url", {})
        marker = "scan-blocked"
        r = await curl(
            f"{fx.base}/{_FX}?marker={marker}",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"

        # The flow lands fully: record complete, body stored.
        flow = await h.wait_flow(marker)
        assert flow["status_code"] == 200
        assert flow["body_file"] is not None
        assert (h.data_dir / flow["body_file"]).is_file()

        # The finding write failed (contained): no findings file, and
        # the flow write path was never touched.
        await asyncio.sleep(0.5)
        assert not (blocked / "findings.jsonl").exists()

        # Capture still works after the failed scan.
        r2 = await curl(
            f"{fx.base}/?marker=scan-after-block",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r2.stdout == "200"
        await h.wait_flow("scan-after-block")


async def test_preboot_flows_are_not_scanned(odda_session, tmp_path) -> None:
    """Flows recorded before session boot are never scanned (forward-only):
    a seeded stack-trace flow predating the session produces no finding."""
    flows = tmp_path / ".odda" / "flows"
    seed_dir = flows / "00001"
    seed_dir.mkdir(parents=True)
    (seed_dir / "request").write_bytes(
        b"GET /checkout?marker=scan-preboot HTTP/1.1\r\nHost: shop.internal\r\n\r\n"
    )
    (seed_dir / "response_headers").write_bytes(
        b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: text/html\r\n\r\n"
    )
    (seed_dir / "response_body.html").write_text(
        "<pre>Traceback (most recent call last):\n</pre>"
    )
    seeded = {
        "id": "00001",
        "method": "GET",
        "scheme": "http",
        "host": "shop.internal",
        "port": 80,
        "path": "/checkout?marker=scan-preboot",
        "browser_id": None,
        "status_code": 500,
        "total_duration_ms": 12.0,
        "body_file": "flows/00001/response_body.html",
        "error": None,
    }
    (flows / "flows.jsonl").write_text(json.dumps(seeded) + "\n")

    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        proxy = await h.call("proxy_url", {})
        r = await curl(
            f"{fx.base}/?marker=scan-postboot",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"
        await h.wait_flow("scan-postboot")

        await asyncio.sleep(0.5)
        # The seeded stack trace was never scanned: no scanner subtree
        # at all, and the history line is untouched.
        assert not (h.data_dir / "scanner").exists()
        assert h.flow_count() == 2
        assert h.flow_record("00001") == seeded


def test_scanner_package_has_no_egress_imports() -> None:
    """Structural zero-egress check: every import in the scanner package
    is on the allowlist — no socket/ssl/urllib/http or any third-party
    module (the scanner is stdlib-only and sends nothing, ADR-0034).
    asyncio is allowlisted for loop primitives (Event, task awaiting);
    egress would require the socket layer, which is not imported."""
    scanner_src = Path(__file__).resolve().parents[2] / "src" / "odda" / "scanner"
    py_files = sorted(scanner_src.rglob("*.py"))
    assert py_files, f"scanner package source not found at {scanner_src}"

    allowed = {
        "__future__",
        "asyncio",  # loop primitives only, never open_connection et al
        "collections",
        "contextlib",
        "dataclasses",
        "enum",
        # ticket 05: dataset-adapter stdlib — one-shot digesting
        # (hashlib), package-data loading (importlib.resources), and a
        # memoized compile (functools). None can open a socket.
        "functools",
        "hashlib",
        "importlib",
        "json",
        "logging",
        "pathlib",
        "queue",
        "re",
        "typing",
        # First-party, package-internal only (odda.scanner.*); the
        # scanner must not reach into flowstore/mitmproxy/mcp.
        "odda",
    }
    imported: set[str] = set()
    for py in py_files:
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])

    assert imported <= allowed, (
        f"scanner imports outside the zero-egress allowlist: "
        f"{sorted(imported - allowed)}"
    )


# --- ticket 05: retirejs dataset ---


def test_retirejs_dataset_ships_offline_as_package_data() -> None:
    """The vendored retire.js repository (Apache-2.0) is the scanner's
    only data source and ships inside the package: the dataset and its
    license resolve through the same importlib.resources mechanism the
    adapter loads them with, and pyproject's package-data ships the
    directory (the userscripts/default precedent)."""
    from importlib import resources

    data = resources.files("odda.scanner.data")
    dataset = data.joinpath("jsrepository.json")
    license_file = data.joinpath("jsrepository.LICENSE")
    assert dataset.is_file(), "vendored jsrepository.json missing from package"
    assert license_file.is_file(), "Apache-2.0 license file missing from package"
    assert "Apache License" in license_file.read_text(encoding="utf-8")

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    assert '"scanner/data/*"' in pyproject.read_text(encoding="utf-8"), (
        "scanner/data must ship via [tool.setuptools.package-data]"
    )


async def test_outdated_jquery_filecontent_lands_finding(odda_session) -> None:
    """A served JS body matching a vendored file-content pattern yields a
    js.outdated-library Finding at the highest matched advisory severity,
    one evidence entry per matched advisory (each naming its own severity),
    with the library, detected version, extractor, and CVE refs inline.

    jquery 1.6.1 against the vendored jquery advisories: exactly seven
    apply — four mediums with floors 1.6.1 clears (1.6.3 and 1.9.0b1
    unbounded, CVE-2020-7656 >= 1.2.1, CVE-2015-9251 >= 1.4.0) plus
    CVE-2019-11358 (>= 1.1.4), CVE-2020-11023 (>= 1.0.3), and the lone
    low at < 2.999.999; the high (CVE-2016-10707) is fenced out by
    atOrAbove 3.0.0-rc.1 — so the aggregate severity is medium."""
    _fx = "ticket05-jquery-old.js"
    async with odda_session() as h, fixture_site([_fx]) as fx:
        proxy = await h.call("proxy_url", {})
        r = await curl(
            f"{fx.base}/{_fx}?marker=scan-t05-jq",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"

        flow = await h.wait_flow("scan-t05-jq")
        finding = await h.wait_finding(flow["id"])

        assert finding["rule_id"] == "js.outdated-library"
        assert finding["flow_id"] == flow["id"]
        assert finding["severity"] == "medium"
        evidence = finding["evidence"]
        assert len(evidence) == 7
        assert all("jquery 1.6.1 (via filecontent)" in e["detail"] for e in evidence)
        assert any("low: " in e["detail"] for e in evidence)
        assert any("medium: " in e["detail"] for e in evidence)
        assert {e["where"] for e in evidence} == {"response_body"}
        assert "CVE-2011-4969" in finding["refs"]
        assert "CVE-2019-11358" in finding["refs"]
        assert "CVE-2020-11023" in finding["refs"]
        assert len(h.findings_for(flow["id"])) == 1


async def test_sha1_hash_extractor_detects_hashed_version(odda_session) -> None:
    """A JS body whose SHA-1 matches a known vulnerable version is
    detected via the hash extractor. The fixture is byte-identical to
    upstream's own retire-example test asset — the preimage of the
    dataset's 07f8b94c… → 0.0.1 hash — and its banner also matches the
    file-content pattern, so this pins the exact-hash-first priority."""
    _fx = "retire-example-0.0.1.min.js"
    async with odda_session() as h, fixture_site([_fx]) as fx:
        proxy = await h.call("proxy_url", {})
        r = await curl(
            f"{fx.base}/{_fx}?marker=scan-t05-hash",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"

        flow = await h.wait_flow("scan-t05-hash")
        finding = await h.wait_finding(flow["id"])

        assert finding["rule_id"] == "js.outdated-library"
        assert finding["severity"] == "low"
        first = finding["evidence"][0]
        assert "retire-example 0.0.1 (via sha1)" in first["detail"]
        assert "vulnerable below 0.0.2" in first["detail"]
        assert first["where"] == "response_body"
        assert first["offset"] == 0
        assert "CVE-XXXX-XXXX" in finding["refs"]


async def test_filename_extractor_detects_path_version(odda_session) -> None:
    """The filename extractor matches the flow record's path: a body
    too inert for any content pattern still lands a finding when the
    URL filename carries the vendored version pattern."""
    _fx = "retire-example-0.0.1.js"
    async with odda_session() as h, fixture_site([_fx]) as fx:
        proxy = await h.call("proxy_url", {})
        r = await curl(
            f"{fx.base}/{_fx}?marker=scan-t05-name",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"

        flow = await h.wait_flow("scan-t05-name")
        finding = await h.wait_finding(flow["id"])

        first = finding["evidence"][0]
        assert "retire-example 0.0.1 (via filename)" in first["detail"]
        assert first["where"] == "request_path"
        assert finding["severity"] == "low"


async def test_current_and_above_threshold_versions_yield_no_finding(
    odda_session,
) -> None:
    """Versions outside the advisory bounds stay silent: a current
    jquery (3.7.1, above every below-bound) and a retire-example at
    the bound itself (0.0.2, not strictly below 0.0.2) produce no
    js.outdated-library finding — while a positive control in the same
    session proves the drain is live."""
    current, at_bound, control = (
        "ticket05-jquery-current.js",
        "retire-example-0.0.2.js",
        "ticket05-jquery-old.js",
    )
    async with odda_session() as h, fixture_site([current, at_bound, control]) as fx:
        proxy = await h.call("proxy_url", {})
        for name, marker in (
            (current, "scan-t05-current"),
            (at_bound, "scan-t05-bound"),
            (control, "scan-t05-control"),
        ):
            r = await curl(
                f"{fx.base}/{name}?marker={marker}",
                proxy,
                extra=["-o", "/dev/null", "-w", "%{http_code}"],
            )
            assert r.stdout == "200"

        control_flow = await h.wait_flow("scan-t05-control")
        await h.wait_finding(control_flow["id"])  # the drain is live

        for marker in ("scan-t05-current", "scan-t05-bound"):
            flow = await h.wait_flow(marker)
            stale = h.findings_for(flow["id"])
            assert not stale, f"no outdated-library finding for {marker}: {stale}"
