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

from tests.e2e.conftest import curl, dyn_server, fixture_site

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


# --- ticket 04: target-confirmation family ---


async def test_shiro_rememberme_delete_me_lands_finding(odda_session) -> None:
    """A ``rememberMe=deleteMe`` Set-Cookie response yields the Shiro
    deserialization Finding (MEDIUM) with response-header evidence; an
    ordinary session cookie on the same server shape does not fire."""
    async with odda_session() as h, dyn_server() as dyn:
        proxy = await h.call("proxy_url", {})
        url = (
            f"{dyn.tls_base}/?marker=t04-shiro"
            "&header=set-cookie:rememberMe%3DdeleteMe%3B%20Path%3D%2F"
            "%3B%20Max-Age%3D0"
        )
        r = await curl(url, proxy, extra=["-o", "/dev/null", "-w", "%{http_code}"])
        assert r.stdout == "200"
        flow = await h.wait_flow("t04-shiro")

        finding = await h.wait_finding(flow["id"])
        assert finding["rule_id"] == "deser.apache-shiro-rememberme"
        assert finding["flow_id"] == flow["id"]
        assert finding["severity"] == "medium"
        assert finding["title"]
        first = finding["evidence"][0]
        assert first["where"] == "response_headers"
        assert isinstance(first["offset"], int)
        assert "rememberMe=deleteMe" in first["detail"]

        # Negative: an ordinary session cookie must not fire the rule.
        clean_url = (
            f"{dyn.tls_base}/?marker=t04-shiro-clean"
            "&header=set-cookie:session%3Dabc123%3B%20Path%3D%2F"
        )
        r2 = await curl(
            clean_url, proxy, extra=["-o", "/dev/null", "-w", "%{http_code}"]
        )
        assert r2.stdout == "200"
        clean = await h.wait_flow("t04-shiro-clean")
        await asyncio.sleep(0.5)
        assert not any(
            f["rule_id"] == "deser.apache-shiro-rememberme"
            for f in h.findings_for(clean["id"])
        )


_S3_HITS = "s3-nosuchbucket.xml"
_S3_MISS = "s3-accessdenied.xml"
_SHOPIFY = "shopify.html"
_CDN_CLEAN = "cdn-clean.html"
_TELERIK_HITS = "telerik-vuln.html"
_TELERIK_MISS = "telerik-unknown.html"
_PROM_HITS = "prometheus.txt"
_PROM_MISS = "prometheus-negative.txt"

# One sample script import per compromised CDN of the 2024 incident:
# polyfill.io plus the BootCDN/Bootcss/Staticfile/Uncdn mirrors the
# same operator was caught serving.
_CDN_IMPORTS = {
    "polyfill.io": "https://polyfill.io/v3/polyfill.min.js",
    "bootcdn.net": "https://cdn.bootcdn.net/jquery/3.7.1/jquery.min.js",
    "bootcss.com": "https://cdn.bootcss.com/jquery/3.7.1/jquery.min.js",
    "staticfile.org": "https://cdn.staticfile.org/jquery/3.7.1/jquery.min.js",
    "staticfile.net": "https://cdn.staticfile.net/jquery/3.7.1/jquery.min.js",
    "uncdn.com": "https://uncdn.com/jquery/3.7.1/jquery.min.js",
}


async def _fetch(h, proxy: str, url: str, marker: str) -> dict:
    """Curl one URL through the proxy; wait for the flow record."""
    r = await curl(url, proxy, extra=["-o", "/dev/null", "-w", "%{http_code}"])
    assert r.stdout == "200", f"curl failed: {r.stdout!r} {r.stderr!r}"
    return await h.wait_flow(marker)


async def _assert_silent(h, flow_id: str, rule_id: str) -> None:
    """Bounded settle for the drain, then the rule fired on no flow of
    this id (the negative-assertion counterpart of wait_finding)."""
    await asyncio.sleep(0.5)
    assert not any(f["rule_id"] == rule_id for f in h.findings_for(flow_id)), (
        f"{rule_id} fired unexpectedly on flow {flow_id}"
    )


async def test_s3_nosuchbucket_fires_and_normal_s3_error_does_not(
    odda_session,
) -> None:
    """An S3 NoSuchBucket body yields the takeover Finding (HIGH) with
    the BucketName value in evidence; a normal S3 XML error
    (AccessDenied) does not fire."""
    async with odda_session() as h, fixture_site([_S3_HITS, _S3_MISS]) as fx:
        proxy = await h.call("proxy_url", {})
        flow = await _fetch(h, proxy, f"{fx.base}/{_S3_HITS}?marker=t04-s3", "t04-s3")

        finding = await h.wait_finding(flow["id"])
        assert finding["rule_id"] == "takeover.s3-nosuchbucket"
        assert finding["flow_id"] == flow["id"]
        assert finding["severity"] == "high"
        first = finding["evidence"][0]
        assert first["where"] == "response_body"
        assert isinstance(first["offset"], int)
        assert "NoSuchBucket" in first["detail"]
        assert "assets.dangling-target.example.com" in first["detail"]

        miss = await _fetch(
            h, proxy, f"{fx.base}/{_S3_MISS}?marker=t04-s3-clean", "t04-s3-clean"
        )
        await _assert_silent(h, miss["id"], "takeover.s3-nosuchbucket")


async def test_shopify_shop_not_found_fires(odda_session) -> None:
    """A Shopify shop-not-found body yields the takeover Finding (HIGH);
    a page without the phrase does not fire."""
    async with odda_session() as h, fixture_site([_SHOPIFY, "index.html"]) as fx:
        proxy = await h.call("proxy_url", {})
        flow = await _fetch(
            h, proxy, f"{fx.base}/{_SHOPIFY}?marker=t04-shopify", "t04-shopify"
        )

        finding = await h.wait_finding(flow["id"])
        assert finding["rule_id"] == "takeover.shopify"
        assert finding["severity"] == "high"
        assert any("currently unavailable" in e["detail"] for e in finding["evidence"])

        miss = await _fetch(
            h,
            proxy,
            f"{fx.base}/index.html?marker=t04-shopify-clean",
            "t04-shopify-clean",
        )
        await _assert_silent(h, miss["id"], "takeover.shopify")


async def test_each_compromised_cdn_import_fires_and_clean_cdns_do_not(
    odda_session,
) -> None:
    """An HTML script import from each compromised CDN yields the
    supply-chain Finding (HIGH) with the domain in evidence; imports
    from unaffected CDNs (cdnjs, unpkg, jsdelivr) do not fire."""
    async with odda_session() as h, fixture_site([_CDN_CLEAN]) as fx:
        proxy = await h.call("proxy_url", {})
        for domain, src in _CDN_IMPORTS.items():
            body = (
                "<!DOCTYPE html>\n<html><head>"
                f'<script src="{src}"></script>'
                "</head><body>cdn import</body></html>\n"
            )
            async with fixture_site(index_body=body) as bad:
                marker = f"t04-cdn-{domain.replace('.', '-')}"
                flow = await _fetch(h, proxy, f"{bad.base}/?marker={marker}", marker)

            finding = await h.wait_finding(flow["id"])
            assert finding["rule_id"] == "js.malicious-cdn-import", domain
            assert finding["severity"] == "high"
            assert domain in finding["evidence"][0]["detail"], domain

        miss = await _fetch(
            h, proxy, f"{fx.base}/{_CDN_CLEAN}?marker=t04-cdn-clean", "t04-cdn-clean"
        )
        await _assert_silent(h, miss["id"], "js.malicious-cdn-import")


async def test_telerik_vulnerable_version_fires_and_unknown_does_not(
    odda_session,
) -> None:
    """A Telerik WebResource reference carrying a vulnerable version
    yields the CVE Finding (HIGH) with CVE-2017-9248 in refs and the
    version in evidence; a fixed/unknown version does not fire."""
    async with (
        odda_session() as h,
        fixture_site([_TELERIK_HITS, _TELERIK_MISS]) as fx,
    ):
        proxy = await h.call("proxy_url", {})
        flow = await _fetch(
            h, proxy, f"{fx.base}/{_TELERIK_HITS}?marker=t04-telerik", "t04-telerik"
        )

        finding = await h.wait_finding(flow["id"])
        assert finding["rule_id"] == "cve.telerik-ui-9248"
        assert finding["severity"] == "high"
        assert "CVE-2017-9248" in finding["refs"]
        assert "2013.2.611.35" in finding["evidence"][0]["detail"]

        miss = await _fetch(
            h,
            proxy,
            f"{fx.base}/{_TELERIK_MISS}?marker=t04-telerik-clean",
            "t04-telerik-clean",
        )
        await _assert_silent(h, miss["id"], "cve.telerik-ui-9248")


async def test_prometheus_metrics_page_fires_and_prose_does_not(
    odda_session,
) -> None:
    """A Prometheus exposition body on a 200 yields the exposure Finding
    (MEDIUM) with the HELP/TYPE pair in evidence; ordinary prose that
    mentions metrics does not fire."""
    async with odda_session() as h, fixture_site([_PROM_HITS, _PROM_MISS]) as fx:
        proxy = await h.call("proxy_url", {})
        flow = await _fetch(
            h, proxy, f"{fx.base}/{_PROM_HITS}?marker=t04-prom", "t04-prom"
        )

        finding = await h.wait_finding(flow["id"])
        assert finding["rule_id"] == "exposure.prometheus-metrics"
        assert finding["severity"] == "medium"
        details = " ".join(e["detail"] for e in finding["evidence"])
        assert "# HELP go_goroutines" in details
        assert "# TYPE go_goroutines gauge" in details

        miss = await _fetch(
            h, proxy, f"{fx.base}/{_PROM_MISS}?marker=t04-prom-clean", "t04-prom-clean"
        )
        await _assert_silent(h, miss["id"], "exposure.prometheus-metrics")
