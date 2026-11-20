# See ADR-0036.
import ast
import asyncio
import itertools
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from types import ModuleType
from urllib.parse import quote

import pytest

from odda.scanner.engine import Scanner
from odda.scanner.gates import _literal_gates, passes_literal_gates
from odda.scanner.model import Evidence, Finding, FlowView, Rule, Severity
from odda.scanner.userrules import RULE_FILE_SKELETON, load_slot_source
from tests.e2e.conftest import (
    _RULES_SRC,
    _findings_index,
    _seed_slot,
    _slot_rules,
    _stored_flow,
    curl,
    dyn_server,
    fixture_site,
)

_FX = "stack-trace.html"


async def test_stack_trace_response_lands_finding(odda_session) -> None:
    async with odda_session() as h, fixture_site([_FX]) as fx:
        await _seed_slot(h, "disclosure")
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
    async with odda_session() as h, fixture_site([_FX]) as fx:
        await _seed_slot(h, "disclosure")
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

        assert len(finding["evidence"]) == 9
        assert len({e["offset"] for e in finding["evidence"]}) == 9
        assert len(h.findings_for(flow["id"])) == 1


async def test_request_send_flow_is_scanned(odda_session) -> None:
    async with odda_session() as h, fixture_site([_FX]) as fx:
        await _seed_slot(h, "disclosure")
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
        rec = await h.call("request_send", {"name": "scan-reqsend", "timeout": 10})
        assert isinstance(rec, dict)

        finding = await h.wait_finding(rec["id"])
        assert finding["rule_id"] == "disclosure.stack-trace"
        assert finding["flow_id"] == rec["id"]


async def test_clean_and_error_flows_produce_no_finding(odda_session) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        await _seed_slot(h, "disclosure")
        proxy = await h.call("proxy_url", {})
        r = await curl(
            f"{fx.base}/?marker=scan-clean",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"
        clean = await h.wait_flow("scan-clean")
        assert clean["error"] is None

        await curl(
            "http://127.0.0.1:9/?marker=scan-err",
            proxy,
            extra=["-o", "/dev/null"],
        )
        err = await h.wait_flow("scan-err")
        assert err["status_code"] is None
        assert err["error"]

        await asyncio.sleep(0.5)
        assert not (h.data_dir / "scanner").exists(), (
            "scanner dir must appear lazily on the first finding"
        )


async def test_scan_failure_never_touches_capture(odda_session) -> None:
    async with odda_session() as h, fixture_site([_FX]) as fx:
        await _seed_slot(h, "disclosure")
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

        flow = await h.wait_flow(marker)
        assert flow["status_code"] == 200
        assert flow["body_file"] is not None
        assert (h.data_dir / flow["body_file"]).is_file()

        await asyncio.sleep(0.5)
        assert not (blocked / "findings.jsonl").exists()

        r2 = await curl(
            f"{fx.base}/?marker=scan-after-block",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r2.stdout == "200"
        await h.wait_flow("scan-after-block")


async def test_preboot_flows_are_not_scanned(odda_session, tmp_path) -> None:
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
        await _seed_slot(h, "disclosure")
        proxy = await h.call("proxy_url", {})
        r = await curl(
            f"{fx.base}/?marker=scan-postboot",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"
        await h.wait_flow("scan-postboot")

        await asyncio.sleep(0.5)
        assert not (h.data_dir / "scanner").exists()
        assert h.flow_count() == 2
        assert h.flow_record("00001") == seeded


def test_scanner_package_has_no_egress_imports() -> None:
    # See ADR-0034.
    scanner_src = Path(__file__).resolve().parents[2] / "src" / "odda" / "scanner"
    py_files = sorted(scanner_src.rglob("*.py"))
    assert py_files, f"scanner package source not found at {scanner_src}"

    allowed = {
        "__future__",
        "asyncio",
        "collections",
        "contextlib",
        "dataclasses",
        "enum",
        "functools",
        "hashlib",
        "importlib",
        "json",
        "logging",
        "pathlib",
        "queue",
        "re",
        "shutil",
        "sys",
        "codecs",
        "time",
        "threading",
        "types",
        "hyperscan",
        "typing",
        "urllib",
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


_PK = "private-key.html"
_PC = "public-cert.html"


async def test_private_key_block_lands_finding(odda_session) -> None:
    async with odda_session() as h, fixture_site([_PK]) as fx:
        await _seed_slot(h, "disclosure")
        proxy = await h.call("proxy_url", {})
        marker = "scan-pk-pos"
        r = await curl(
            f"{fx.base}/{_PK}?marker={marker}",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"

        flow = await h.wait_flow(marker)
        finding = await h.wait_finding(flow["id"])

        assert finding["rule_id"] == "disclosure.private-key"
        assert finding["flow_id"] == flow["id"]
        assert finding["severity"] == "high"
        assert finding["title"]
        assert "CWE-321" in finding["refs"]
        assert len(finding["evidence"]) == 2
        details = {e["detail"] for e in finding["evidence"]}
        assert any("RSA PRIVATE KEY" in d for d in details)
        assert any("OPENSSH PRIVATE KEY" in d for d in details)
        assert all(e["where"] == "response_body" for e in finding["evidence"])
        assert len({e["offset"] for e in finding["evidence"]}) == 2
        assert len(h.findings_for(flow["id"])) == 1


async def test_public_certificate_block_does_not_fire(odda_session) -> None:
    async with odda_session() as h, fixture_site([_PC]) as fx:
        await _seed_slot(h, "disclosure")
        proxy = await h.call("proxy_url", {})
        marker = "scan-pk-neg"
        r = await curl(
            f"{fx.base}/{_PC}?marker={marker}",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"

        flow = await h.wait_flow(marker)
        await asyncio.sleep(0.5)
        assert h.findings_for(flow["id"]) == []


_FS = "frontend-secret.js"
_BK = "benign-keynames.js"


async def test_frontend_secret_assignment_lands_finding(odda_session) -> None:
    async with odda_session() as h, fixture_site([_FS]) as fx:
        await _seed_slot(h, "disclosure")
        proxy = await h.call("proxy_url", {})
        marker = "scan-fs-pos"
        r = await curl(
            f"{fx.base}/{_FS}?marker={marker}",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"

        flow = await h.wait_flow(marker)
        finding = await h.wait_finding(flow["id"])

        assert finding["rule_id"] == "disclosure.frontend-secret"
        assert finding["flow_id"] == flow["id"]
        assert finding["severity"] == "medium"
        assert finding["title"]
        assert "CWE-798" in finding["refs"]
        assert len(finding["evidence"]) == 6
        details = " ".join(e["detail"] for e in finding["evidence"])
        assert "client_secret" in details
        assert "refreshToken" in details
        assert "api_key" in details
        assert "apiKey" in details
        assert details.count("password") == 2
        assert all(e["where"] == "response_body" for e in finding["evidence"])
        assert len(h.findings_for(flow["id"])) == 1


async def test_benign_key_names_do_not_fire(odda_session) -> None:
    async with odda_session() as h, fixture_site([_BK]) as fx:
        await _seed_slot(h, "disclosure")
        proxy = await h.call("proxy_url", {})
        marker = "scan-fs-neg"
        r = await curl(
            f"{fx.base}/{_BK}?marker={marker}",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"

        flow = await h.wait_flow(marker)
        await asyncio.sleep(0.5)
        assert h.findings_for(flow["id"]) == []
        assert not (h.data_dir / "scanner").exists()


async def test_frontend_secret_requires_2xx(odda_session) -> None:
    async with odda_session() as h, dyn_server() as dyn:
        await _seed_slot(h, "disclosure")
        proxy = await h.call("proxy_url", {})
        secret = 'const apiKey = "sk_live_26XyZ9Q8pL2mN4vB7cD3eF5g";'
        url = (
            f"{dyn.tls_base}/?marker=scan-fs-404"
            "&status=404"
            "&header=content-type:text/javascript"
            f"&body={quote(secret, safe='')}"
        )
        r = await curl(
            url,
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "404"

        flow = await h.wait_flow("scan-fs-404")
        await asyncio.sleep(0.5)
        assert h.findings_for(flow["id"]) == []


_AWB = "aws-key-body.txt"


async def test_aws_key_in_body_lands_finding(odda_session) -> None:
    async with odda_session() as h, fixture_site([_AWB]) as fx:
        await _seed_slot(h, "disclosure")
        proxy = await h.call("proxy_url", {})
        marker = "scan-aws-body"
        r = await curl(
            f"{fx.base}/{_AWB}?marker={marker}",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"

        flow = await h.wait_flow(marker)
        finding = await h.wait_finding(flow["id"])

        assert finding["rule_id"] == "disclosure.aws-key"
        assert finding["flow_id"] == flow["id"]
        assert finding["severity"] == "high"
        assert finding["title"]
        assert "CWE-798" in finding["refs"]
        assert len(finding["evidence"]) == 1
        first = finding["evidence"][0]
        assert first["where"] == "response_body"
        assert first["detail"] == "AKIAIOSFODNN7EXAMPLE"
        assert len(h.findings_for(flow["id"])) == 1


async def test_aws_key_in_response_header_lands_finding(odda_session) -> None:
    async with odda_session() as h, dyn_server() as dyn:
        await _seed_slot(h, "disclosure")
        proxy = await h.call("proxy_url", {})
        url = (
            f"{dyn.tls_base}/?marker=scan-aws-hdr"
            "&header=x-access-key:AKIAIOSFODNN7EXAMPLE"
        )
        r = await curl(
            url,
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"

        flow = await h.wait_flow("scan-aws-hdr")
        finding = await h.wait_finding(flow["id"])

        assert finding["rule_id"] == "disclosure.aws-key"
        assert finding["flow_id"] == flow["id"]
        assert finding["severity"] == "high"
        assert len(finding["evidence"]) == 1
        first = finding["evidence"][0]
        assert first["where"] == "response_headers"
        assert first["detail"] == "AKIAIOSFODNN7EXAMPLE"
        assert isinstance(first["offset"], int)
        assert len(h.findings_for(flow["id"])) == 1


async def test_xdebug_setcookie_lands_finding(odda_session) -> None:
    async with odda_session() as h, dyn_server() as dyn:
        await _seed_slot(h, "cookies")
        proxy = await h.call("proxy_url", {})
        url = (
            f"{dyn.tls_base}/?marker=scan-xdbg-pos"
            f"&header=set-cookie:{quote('XDEBUG_SESSION=odda; path=/', safe='')}"
        )
        r = await curl(
            url,
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"

        flow = await h.wait_flow("scan-xdbg-pos")
        finding = await h.wait_finding(flow["id"])

        assert finding["rule_id"] == "cookie.xdebug-session"
        assert finding["flow_id"] == flow["id"]
        assert finding["severity"] == "high"
        assert any("xdebug.org" in ref for ref in finding["refs"])
        assert len(finding["evidence"]) == 1
        first = finding["evidence"][0]
        assert first["where"] == "response_headers"
        assert "XDEBUG_SESSION=" in first["detail"]
        assert len(h.findings_for(flow["id"])) == 1


async def test_ordinary_setcookie_does_not_fire(odda_session) -> None:
    async with odda_session() as h, dyn_server() as dyn:
        await _seed_slot(h, "cookies")
        proxy = await h.call("proxy_url", {})
        url = (
            f"{dyn.tls_base}/?marker=scan-xdbg-neg"
            f"&header=set-cookie:{quote('sessionid=abc123; Path=/', safe='')}"
        )
        r = await curl(
            url,
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"

        flow = await h.wait_flow("scan-xdbg-neg")
        await asyncio.sleep(0.5)
        assert h.findings_for(flow["id"]) == []


async def test_shiro_rememberme_delete_me_lands_finding(odda_session) -> None:
    async with odda_session() as h, dyn_server() as dyn:
        await _seed_slot(h, "fingerprints")
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

_CDN_IMPORTS = {
    "polyfill.io": "https://polyfill.io/v3/polyfill.min.js",
    "bootcdn.net": "https://cdn.bootcdn.net/jquery/3.7.1/jquery.min.js",
    "bootcss.com": "https://cdn.bootcss.com/jquery/3.7.1/jquery.min.js",
    "staticfile.org": "https://cdn.staticfile.org/jquery/3.7.1/jquery.min.js",
    "staticfile.net": "https://cdn.staticfile.net/jquery/3.7.1/jquery.min.js",
    "uncdn.com": "https://uncdn.com/jquery/3.7.1/jquery.min.js",
}


async def _fetch(h, proxy: str, url: str, marker: str) -> dict:
    r = await curl(url, proxy, extra=["-o", "/dev/null", "-w", "%{http_code}"])
    assert r.stdout == "200", f"curl failed: {r.stdout!r} {r.stderr!r}"
    return await h.wait_flow(marker)


async def _assert_silent(h, flow_id: str, rule_id: str) -> None:
    await asyncio.sleep(0.5)
    assert not any(f["rule_id"] == rule_id for f in h.findings_for(flow_id)), (
        f"{rule_id} fired unexpectedly on flow {flow_id}"
    )


async def test_s3_nosuchbucket_fires_and_normal_s3_error_does_not(
    odda_session,
) -> None:
    async with odda_session() as h, fixture_site([_S3_HITS, _S3_MISS]) as fx:
        await _seed_slot(h, "takeover")
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
    async with odda_session() as h, fixture_site([_SHOPIFY, "index.html"]) as fx:
        await _seed_slot(h, "takeover")
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
    async with odda_session() as h, fixture_site([_CDN_CLEAN]) as fx:
        await _seed_slot(h, "supplychain")
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
    async with (
        odda_session() as h,
        fixture_site([_TELERIK_HITS, _TELERIK_MISS]) as fx,
    ):
        await _seed_slot(h, "fingerprints")
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
    async with odda_session() as h, fixture_site([_PROM_HITS, _PROM_MISS]) as fx:
        await _seed_slot(h, "fingerprints")
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


async def test_outdated_jquery_filecontent_lands_finding(odda_session) -> None:
    _fx = "ticket05-jquery-old.js"
    async with odda_session() as h, fixture_site([_fx]) as fx:
        await _seed_slot(h, "retirejs")
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
    _fx = "retire-example-0.0.1.min.js"
    async with odda_session() as h, fixture_site([_fx]) as fx:
        await _seed_slot(h, "retirejs")
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
    _fx = "retire-example-0.0.1.js"
    async with odda_session() as h, fixture_site([_fx]) as fx:
        await _seed_slot(h, "retirejs")
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
    current, at_bound, control = (
        "ticket05-jquery-current.js",
        "retire-example-0.0.2.js",
        "ticket05-jquery-old.js",
    )
    async with odda_session() as h, fixture_site([current, at_bound, control]) as fx:
        await _seed_slot(h, "retirejs")
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
        await h.wait_finding(control_flow["id"])

        for marker in ("scan-t05-current", "scan-t05-bound"):
            flow = await h.wait_flow(marker)
            stale = h.findings_for(flow["id"])
            assert not stale, f"no outdated-library finding for {marker}: {stale}"


async def test_filecontent_scans_through_the_seam_in_one_pass(
    odda_session, monkeypatch
) -> None:
    from odda.scanner import fastscan

    calls: list[str] = []
    unspoiled = fastscan.FastScan.find_all

    def counting(self: fastscan.FastScan, text: str) -> list:
        calls.append(text)
        return unspoiled(self, text)

    monkeypatch.setattr(fastscan.FastScan, "find_all", counting)

    _fx = "ticket05-jquery-old.js"
    async with odda_session() as h, fixture_site([_fx]) as fx:
        await _seed_slot(h, "retirejs")
        proxy = await h.call("proxy_url", {})
        r = await curl(
            f"{fx.base}/{_fx}?marker=scan-t17-seam",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"

        flow = await h.wait_flow("scan-t17-seam")
        finding = await h.wait_finding(flow["id"])

        assert finding["rule_id"] == "js.outdated-library"
        assert finding["severity"] == "medium"
        assert len(finding["evidence"]) == 7
        assert all(
            "jquery 1.6.1 (via filecontent)" in e["detail"] for e in finding["evidence"]
        )
        passes = [text for text in calls if "jQuery v1.6.1" in text]
        assert len(passes) == 1, (
            f"expected exactly one seam pass over the jquery body, got {len(passes)}"
        )


def test_refused_and_prefiltered_filecontent_patterns_still_detect(
    tmp_path,
) -> None:
    _entry = {
        "extractors": {"filecontent": ["PATTERN"]},
        "vulnerabilities": [
            {
                "severity": "high",
                "below": "99",
                "identifiers": {"summary": "t17 crafted advisory"},
                "info": [],
            }
        ],
    }
    dataset = {
        "plain": {**_entry, "extractors": {"filecontent": ["plain-v§§version§§"]}},
        "loose": {**_entry, "extractors": {"filecontent": ["§§version§§?"]}},
        "echo": {
            **_entry,
            "extractors": {"filecontent": ["rev=§§version§§ echo:\\1"]},
        },
    }
    slot = tmp_path / "t17-craft"
    slot.mkdir()
    (slot / "rule.py").write_text(
        (_RULES_SRC / "retirejs" / "rule.py").read_text(encoding="utf-8")
    )
    (slot / "jsrepository.json").write_text(json.dumps(dataset))

    module = ModuleType("t17.slot.crafted")
    module.__file__ = str(slot / "rule.py")
    sys.modules[module.__name__] = module
    try:
        exec(  # noqa: S102
            compile((slot / "rule.py").read_text(encoding="utf-8"), str(slot), "exec"),
            module.__dict__,
        )
    finally:
        del sys.modules[module.__name__]
    rule = module.__dict__["RULES"][0]

    data_dir = tmp_path / ".odda"
    for marker, body, library, version, offset in (
        ("plain", b"banner plain-v1.2.3 tail", "plain", "1.2.3", 7),
        ("loose", b"7.0.0beta rest", "loose", "7.0.0beta", 0),
        ("echo", b"pre rev=2.1 echo:2.1 post", "echo", "2.1", 4),
    ):
        record = _stored_flow(
            data_dir, f"t17{marker}", body, content_type="application/javascript"
        )
        findings = rule.scan(FlowView(data_dir, record))
        assert len(findings) == 1, marker
        finding = findings[0]
        assert finding.rule_id == "js.outdated-library"
        assert finding.severity is Severity.HIGH
        assert finding.evidence[0].where == "response_body"
        assert finding.evidence[0].offset == offset, marker
        assert f"{library} {version} (via filecontent)" in finding.evidence[0].detail


_T06_CANARY = "odda-ticket06-canary"
_T06_BODY = (
    "<html><body>ticket06 page; Traceback (most recent call last): "
    f'File "/app/t06.py"; canary {_T06_CANARY} present</body></html>\n'
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

_RULE_COLLIDING = """from odda.scanner import Rule, Severity

RULES = [
    Rule(
        id="disclosure.stack-trace",
        severity=Severity.MEDIUM,
        title="Collides with the disclosure slot's id",
        body_patterns=(r"never-matches",),
    ),
]
"""

_RULE_DUP = f'''from odda.scanner import Rule, Severity

RULES = [
    Rule(
        id="user.ticket06-dup",
        severity=Severity.LOW,
        title="Ticket06 duplicate-id probe",
        body_patterns=(r"{_T06_CANARY}",),
    ),
]
'''

_RULE_BROKEN = "this is not python {{{"

_RULE_PACK = f'''from odda.scanner import Rule, Severity


class _Boom(str):
    __hash__ = str.__hash__

    def __eq__(self, other):
        raise RuntimeError("ticket06 rule boom")


RULES = [
    Rule(
        id="user.ticket06-sibling",
        severity=Severity.LOW,
        title="Ticket06 sibling marker disclosed",
        body_patterns=(r"{_T06_CANARY}",),
    ),
    Rule(
        id="user.ticket06-raiser",
        severity=Severity.LOW,
        title="Raises while gating",
        schemes=(_Boom("http"),),
        body_patterns=(r"{_T06_CANARY}",),
    ),
]
'''


def _slot_path(h, name: str) -> Path:
    return h.data_dir / "rules" / name / "rule.py"


async def _drive(h, fx, marker: str) -> dict:
    proxy = await h.call("proxy_url", {})
    await curl(f"{fx.base}/?marker={marker}", proxy, extra=["-o", "/dev/null"])
    return await h.wait_flow(marker)


async def _wait_rule_finding(h, flow_id: str, rule_id: str) -> dict:
    for _ in range(200):
        for f in h.findings_for(flow_id):
            if f["rule_id"] == rule_id:
                return f
        await asyncio.sleep(0.05)
    raise AssertionError(f"no {rule_id!r} finding for flow {flow_id!r}")


async def test_rule_install_applies_to_flows_after_install_only(
    odda_session,
) -> None:
    async with odda_session() as h, fixture_site(index_body=_T06_BODY) as fx:
        pre = await _drive(h, fx, "t06-pre-install")

        r = await h.call("rule_install", {"name": "canary", "source": _RULE_CANARY})
        assert r["name"] == "canary"
        assert r["size"] > 0
        assert r["rules"] == ["user.ticket06-canary"]
        assert _slot_path(h, "canary").is_file()

        post = await _drive(h, fx, "t06-post-install")
        finding = await _wait_rule_finding(h, post["id"], "user.ticket06-canary")
        assert finding["flow_id"] == post["id"]
        assert finding["severity"] == "medium"
        assert finding["title"] == "Ticket06 canary marker disclosed"
        assert finding["evidence"][0]["where"] == "response_body"
        assert _T06_CANARY in finding["evidence"][0]["detail"]

        assert all(
            f["rule_id"] != "user.ticket06-canary" for f in h.findings_for(pre["id"])
        )


async def test_rule_install_from_file_and_rule_list_rows(odda_session) -> None:
    async with odda_session() as h:
        r = await _seed_slot(h, "disclosure")
        assert r["name"] == "disclosure"
        assert r["size"] > 0
        assert r["rules"] == [
            "disclosure.stack-trace",
            "disclosure.private-key",
            "disclosure.frontend-secret",
            "disclosure.aws-key",
        ]
        assert "disclosure.stack-trace" in _slot_path(h, "disclosure").read_text()

        rows = await h.call("rule_list", {})
        assert rows == [
            {
                "slot": "disclosure",
                "id": rule_id,
                "severity": severity,
                "scope": "project",
                "state": "active",
                "error": None,
            }
            for rule_id, severity in (
                ("disclosure.stack-trace", "high"),
                ("disclosure.private-key", "high"),
                ("disclosure.frontend-secret", "medium"),
                ("disclosure.aws-key", "high"),
            )
        ]


async def test_syntax_error_install_rejected_with_expected_shape(
    odda_session,
) -> None:
    async with odda_session() as h:
        err = await h.call_error(
            "rule_install", {"name": "bad", "source": _RULE_BROKEN}
        )
        assert "Failed to load rule slot 'bad'" in err
        assert "SyntaxError" in err
        assert RULE_FILE_SKELETON in err
        assert not _slot_path(h, "bad").exists()


async def test_colliding_rule_ids_rejected_naming_the_collision(
    odda_session,
) -> None:
    async with odda_session() as h:
        await _seed_slot(h, "disclosure")
        err = await h.call_error(
            "rule_install", {"name": "clash-slot", "source": _RULE_COLLIDING}
        )
        assert "Rule id 'disclosure.stack-trace'" in err
        assert "already registered by rule slot 'disclosure'" in err
        assert not _slot_path(h, "clash-slot").exists()

        await h.call("rule_install", {"name": "dup-a", "source": _RULE_DUP})
        err = await h.call_error("rule_install", {"name": "dup-b", "source": _RULE_DUP})
        assert "Rule id 'user.ticket06-dup'" in err
        assert "'dup-a'" in err
        assert not _slot_path(h, "dup-b").exists()


async def test_rule_remove_keeps_past_findings_stops_future(odda_session) -> None:
    async with odda_session() as h, fixture_site(index_body=_T06_BODY) as fx:
        await _seed_slot(h, "disclosure")
        await h.call("rule_install", {"name": "canary", "source": _RULE_CANARY})
        before = await _drive(h, fx, "t06-remove-before")
        await _wait_rule_finding(h, before["id"], "user.ticket06-canary")

        r = await h.call("rule_remove", {"name": "canary"})
        assert r == {
            "name": "canary",
            "scope": "project",
            "removed": True,
            "rules": ["user.ticket06-canary"],
        }
        assert not _slot_path(h, "canary").parent.exists()

        after = await _drive(h, fx, "t06-remove-after")
        await _wait_rule_finding(h, after["id"], "disclosure.stack-trace")
        assert all(
            f["rule_id"] != "user.ticket06-canary" for f in h.findings_for(after["id"])
        )
        assert any(
            f["rule_id"] == "user.ticket06-canary" for f in h.findings_for(before["id"])
        )


async def test_raising_rule_is_contained_per_rule(odda_session) -> None:
    async with odda_session() as h, fixture_site(index_body=_T06_BODY) as fx:
        await _seed_slot(h, "disclosure")
        r = await h.call("rule_install", {"name": "pack", "source": _RULE_PACK})
        assert r["rules"] == ["user.ticket06-sibling", "user.ticket06-raiser"]

        flow = await _drive(h, fx, "t06-raiser-boom")
        flow_dir = h.data_dir / "flows" / flow["id"]
        assert (flow_dir / "request").is_file()
        assert (flow_dir / "response_headers").is_file()
        assert (flow_dir / "response_body.html").is_file()

        await _wait_rule_finding(h, flow["id"], "disclosure.stack-trace")
        await _wait_rule_finding(h, flow["id"], "user.ticket06-sibling")
        assert all(
            f["rule_id"] != "user.ticket06-raiser" for f in h.findings_for(flow["id"])
        )


async def test_rule_tool_validation_errors_verbatim(odda_session, tmp_path) -> None:
    rf = tmp_path / "t06_canary_rule.py"
    rf.write_text(_RULE_CANARY)
    async with odda_session() as h:
        err = await h.call_error(
            "rule_install", {"name": "both", "file": str(rf), "source": "1"}
        )
        assert "Provide either file or source, not both" in err

        err = await h.call_error("rule_install", {"name": "neither"})
        assert "Provide file <path> or source <py>" in err

        err = await h.call_error(
            "rule_install",
            {"name": "missing", "file": str(tmp_path / "does-not-exist.py")},
        )
        assert "File not found:" in err

        err = await h.call_error("rule_install", {"name": "empty", "source": "   "})
        assert "source is empty" in err

        err = await h.call_error("rule_remove", {"name": "nope"})
        assert "Rule slot 'nope' not found" in err

        await h.call("rule_install", {"name": "taken", "source": _RULE_CANARY})
        err = await h.call_error("rule_install", {"name": "taken", "source": _RULE_ALT})
        assert "Rule slot 'taken' already installed. Use force to overwrite." in err


async def test_force_overwrite_swaps_the_slot_live(odda_session) -> None:
    async with odda_session() as h, fixture_site(index_body=_T06_BODY) as fx:
        await h.call("rule_install", {"name": "swap", "source": _RULE_CANARY})
        r = await h.call(
            "rule_install", {"name": "swap", "source": _RULE_ALT, "force": True}
        )
        assert r["rules"] == ["user.ticket06-alt"]
        assert "user.ticket06-alt" in _slot_path(h, "swap").read_text()

        rows = await h.call("rule_list", {})
        ids = {row["id"] for row in rows}
        assert "user.ticket06-alt" in ids
        assert "user.ticket06-canary" not in ids

        flow = await _drive(h, fx, "t06-force-swap")
        await _wait_rule_finding(h, flow["id"], "user.ticket06-alt")
        assert all(
            f["rule_id"] != "user.ticket06-canary" for f in h.findings_for(flow["id"])
        )


# See ADR-0036.
_SLOT_FAMILIES = (
    "cookies",
    "disclosure",
    "fingerprints",
    "request",
    "retirejs",
    "supplychain",
    "takeover",
)


def _all_slot_rules() -> list[Rule]:
    return [rule for family in _SLOT_FAMILIES for rule in _slot_rules(family)]


def _slot_module(family: str) -> dict:
    path = _RULES_SRC / family / "rule.py"
    module = ModuleType(f"t16.slot.{family}")
    module.__file__ = str(path)
    sys.modules[module.__name__] = module
    try:
        exec(  # noqa: S102
            compile(path.read_text(encoding="utf-8"), str(path), "exec"),
            module.__dict__,
        )
    finally:
        del sys.modules[module.__name__]
    return module.__dict__


async def test_rule_install_from_file_copies_sibling_assets(
    odda_session, tmp_path
) -> None:
    src = tmp_path / "t16-dataset-slot"
    src.mkdir()
    rule_file = src / "rule.py"
    rule_file.write_text(_RULE_CANARY)
    (src / "dataset.json").write_text('{"t16": true}')
    (src / "NOTICE.txt").write_text("t16 asset")
    (src / "helper.py").write_text("# a .py sibling never rides along\n")
    (src / "subdir").mkdir()

    async with odda_session() as h:
        r = await h.call("rule_install", {"name": "assets", "file": str(rule_file)})
        assert r["rules"] == ["user.ticket06-canary"]
        assert r["assets"] == ["NOTICE.txt", "dataset.json"]
        slot_dir = _slot_path(h, "assets").parent
        assert _slot_path(h, "assets").is_file()
        assert (slot_dir / "dataset.json").read_text(
            encoding="utf-8"
        ) == '{"t16": true}'
        assert (slot_dir / "NOTICE.txt").is_file()
        assert not (slot_dir / "helper.py").exists(), ".py siblings stay behind"
        assert not (slot_dir / "subdir").exists(), "subdirectories stay behind"

        plain = tmp_path / "t16-plain.py"
        plain.write_text(_RULE_ALT)
        r2 = await h.call(
            "rule_install", {"name": "assets", "file": str(plain), "force": True}
        )
        assert r2["assets"] == []
        assert not (slot_dir / "dataset.json").exists()
        assert not (slot_dir / "NOTICE.txt").exists()
        assert "user.ticket06-alt" in _slot_path(h, "assets").read_text()


async def test_empty_boot_scans_nothing_and_captures_fine(odda_session) -> None:
    # See ADR-0036.
    async with odda_session() as h, fixture_site([_FX, "index.html"]) as fx:
        assert await h.call("rule_list", {}) == []

        proxy = await h.call("proxy_url", {})
        marker = "t16-empty-clean"
        r = await curl(
            f"{fx.base}/index.html?marker={marker}",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"
        clean = await h.wait_flow(marker)
        assert clean["error"] is None

        r = await curl(
            f"{fx.base}/{_FX}?marker=t16-empty-match",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"
        match = await h.wait_flow("t16-empty-match")
        assert match["body_file"] is not None

        await curl(
            "http://127.0.0.1:9/?marker=t16-empty-err",
            proxy,
            extra=["-o", "/dev/null"],
        )
        err = await h.wait_flow("t16-empty-err")
        assert err["error"]

        await asyncio.sleep(0.5)
        assert not (h.data_dir / "scanner").exists(), (
            "empty boot must not write a findings index"
        )
        assert h.findings_for(clean["id"]) == []
        assert h.findings_for(match["id"]) == []
        assert h.findings_for(err["id"]) == []
        assert await h.call("rule_list", {}) == []


def test_package_ships_no_rules_or_datasets() -> None:
    # See ADR-0036.
    scanner_src = Path(__file__).resolve().parents[2] / "src" / "odda" / "scanner"
    names = {p.name for p in scanner_src.iterdir()}
    assert "builtins" not in names, "no builtin rule modules may ship"
    assert "data" not in names, "no rule datasets may ship"

    with pytest.raises(ImportError):
        from odda.scanner import builtins  # noqa: F401

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    assert '"scanner/data/*"' not in pyproject.read_text(encoding="utf-8"), (
        "scanner package-data entry must be gone"
    )
    assert (_RULES_SRC / "retirejs" / "jsrepository.json").is_file()


async def test_slots_persist_and_load_on_next_boot_bad_slot_skipped(
    odda_session, config_home
) -> None:
    async with odda_session() as h:
        await h.call("rule_install", {"name": "keeper", "source": _RULE_CANARY})
        assert _slot_path(h, "keeper").is_file()

    broken = config_home / "rules" / "broken" / "rule.py"
    broken.parent.mkdir(parents=True)
    broken.write_text(_RULE_BROKEN)

    async with odda_session() as h, fixture_site(index_body=_T06_BODY) as fx:
        rows = await h.call("rule_list", {})
        failed = [row for row in rows if row["state"] == "failed"]
        assert len(failed) == 1
        assert failed[0]["slot"] == "broken"
        assert failed[0]["scope"] == "global"
        assert "SyntaxError" in failed[0]["error"]

        keeper = next(row for row in rows if row["id"] == "user.ticket06-canary")
        assert keeper == {
            "slot": "keeper",
            "id": "user.ticket06-canary",
            "severity": "medium",
            "scope": "project",
            "state": "active",
            "error": None,
        }

        flow = await _drive(h, fx, "t06-boot-restored")
        finding = await _wait_rule_finding(h, flow["id"], "user.ticket06-canary")
        assert finding["flow_id"] == flow["id"]


_JS = "jsessionid=t03-nav-session"
_PW = "pwd=t03-nav-password"


async def _drained_silent(h, fx, marker: str, *flow_ids: str) -> None:
    proxy = await h.call("proxy_url", {})
    await curl(f"{fx.base}/{_FX}?marker={marker}", proxy, extra=["-o", "/dev/null"])
    probe = await h.wait_flow(marker)
    await h.wait_finding(probe["id"])
    for flow_id in flow_ids:
        assert h.findings_for(flow_id) == [], f"flow {flow_id}: unexpected findings"


async def test_browser_session_and_password_params_in_url_land_findings(
    odda_session,
) -> None:
    async with odda_session() as h, fixture_site(["index.html"]) as fx:
        await _seed_slot(h, "request")
        bid, tid = await h.open_browser()
        await h.navigate(bid, tid, f"{fx.base}/?marker=t03-nav&{_JS}&{_PW}")
        flow = await h.wait_flow("t03-nav")
        assert flow["browser_id"] is not None

        token = await _wait_rule_finding(h, flow["id"], "request.session-token-in-url")
        assert token["severity"] == "medium"
        assert token["flow_id"] == flow["id"]
        ev = token["evidence"][0]
        assert ev["where"] == "request_query"
        assert isinstance(ev["offset"], int)
        assert ev["detail"].startswith("jsessionid=")

        pw = await _wait_rule_finding(h, flow["id"], "request.password-in-url")
        assert pw["severity"] == "medium"
        assert pw["evidence"][0]["where"] == "request_query"
        assert pw["evidence"][0]["detail"].startswith("pwd=")


async def test_request_send_probe_is_invisible_to_request_rules(
    odda_session,
) -> None:
    async with odda_session() as h, fixture_site([_FX, "index.html"]) as fx:
        await _seed_slot(h, "disclosure")
        await _seed_slot(h, "request")
        await h.call(
            "request_new",
            {
                "name": "t03-probe",
                "host": "127.0.0.1",
                "protocol": "http",
                "port": fx.port,
            },
        )
        h.write_request(
            "t03-probe",
            (
                f"GET {fx.path}/?marker=t03-probe&{_JS}&{_PW} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{fx.port}\r\n"
                "Authorization: Bearer t03-probe-creds\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode(),
        )
        rec = await h.call("request_send", {"name": "t03-probe", "timeout": 10})
        assert rec["browser_id"] is None

        await _drained_silent(h, fx, "t03-probe-settle", rec["id"])


async def test_browser_cleartext_authorization_http_only(odda_session) -> None:
    async with (
        odda_session() as h,
        fixture_site([_FX, "index.html"]) as fx,
        dyn_server() as dyn,
    ):
        await _seed_slot(h, "disclosure")
        await _seed_slot(h, "request")
        bid, tid = await h.open_browser(f"{fx.base}/")

        status = await h.eval(
            bid,
            tid,
            "fetch('?marker=t03-auth-http', "
            "{headers: {Authorization: 'Bearer t03-http'}})"
            ".then(r => r.status)",
        )
        assert status == "200"
        http_flow = await h.wait_flow("t03-auth-http")
        assert http_flow["scheme"] == "http"
        assert http_flow["browser_id"] is not None

        finding = await _wait_rule_finding(
            h, http_flow["id"], "request.cleartext-authorization"
        )
        assert finding["severity"] == "high"
        ev = finding["evidence"][0]
        assert ev["where"] == "request_headers"
        assert ev["detail"].startswith("Authorization:")

        await h.navigate(bid, tid, f"{dyn.tls_base}/")
        status = await h.eval(
            bid,
            tid,
            "fetch('?marker=t03-auth-https', "
            "{headers: {Authorization: 'Bearer t03-https'}})"
            ".then(r => r.status)",
        )
        assert status == "200"
        https_flow = await h.wait_flow("t03-auth-https")
        assert https_flow["scheme"] == "https"
        assert https_flow["browser_id"] is not None

        proxy = await h.call("proxy_url", {})
        await curl(
            f"{fx.base}/?marker=t03-auth-curl",
            proxy,
            extra=["-H", "Authorization: Bearer t03-curl", "-o", "/dev/null"],
        )
        curl_flow = await h.wait_flow("t03-auth-curl")
        assert curl_flow["browser_id"] is None

        await _drained_silent(
            h, fx, "t03-auth-settle", https_flow["id"], curl_flow["id"]
        )


async def test_browser_xdebug_session_start_param_lands_finding(
    odda_session,
) -> None:
    async with odda_session() as h, fixture_site([_FX, "index.html"]) as fx:
        await _seed_slot(h, "cookies")
        await _seed_slot(h, "disclosure")
        bid, tid = await h.open_browser()
        await h.navigate(
            bid, tid, f"{fx.base}/?marker=t03-xdebug&XDEBUG_SESSION_START=t03"
        )
        flow = await h.wait_flow("t03-xdebug")
        assert flow["browser_id"] is not None

        finding = await _wait_rule_finding(h, flow["id"], "cookie.xdebug-session")
        assert finding["severity"] == "high"
        ev = finding["evidence"][0]
        assert ev["where"] == "request_query"
        assert ev["detail"].lower().startswith("xdebug_session_start=")

        proxy = await h.call("proxy_url", {})
        await curl(
            f"{fx.base}/?marker=t03-xdebug-curl&XDEBUG_SESSION_START=t03",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        curl_flow = await h.wait_flow("t03-xdebug-curl")
        assert curl_flow["browser_id"] is None
        await _drained_silent(h, fx, "t03-xdebug-settle", curl_flow["id"])


async def test_error_flow_from_browser_keeps_request_rules(odda_session) -> None:
    async with odda_session() as h:
        await _seed_slot(h, "request")
        bid, tid = await h.open_browser()
        await h.client.call_tool(
            "navigate",
            {
                "browser_id": bid,
                "tab_id": tid,
                "url": "http://odda-dead.invalid/?marker=t03-dead&token=t03-dead-token",
            },
        )
        flow = await h.wait_flow("t03-dead")
        assert flow["status_code"] is None
        assert flow["error"]
        assert flow["browser_id"] is not None

        finding = await _wait_rule_finding(
            h, flow["id"], "request.session-token-in-url"
        )
        assert finding["evidence"][0]["detail"].startswith("token=")


_STATUS_ONLY = ["-o", "/dev/null", "-w", "%{http_code}"]


async def test_sequential_sessions_are_forward_only(odda_session, tmp_path) -> None:
    data_dir = tmp_path / ".odda"
    async with fixture_site([_FX, "index.html"]) as fx:
        async with odda_session() as a:
            await _seed_slot(a, "disclosure")
            proxy = await a.call("proxy_url", {})
            r = await curl(
                f"{fx.base}/{_FX}?marker=t07-seq-a", proxy, extra=_STATUS_ONLY
            )
            assert r.stdout == "200"
            flow_a = await a.wait_flow("t07-seq-a")
            # See ADR-0034.
            finding_a = await a.wait_finding(flow_a["id"])

        assert _findings_index(data_dir) == [finding_a]

        async with odda_session() as b:
            proxy = await b.call("proxy_url", {})
            r = await curl(
                f"{fx.base}/index.html?marker=t07-seq-b-quiet",
                proxy,
                extra=_STATUS_ONLY,
            )
            assert r.stdout == "200"
            quiet = await b.wait_flow("t07-seq-b-quiet")
            r = await curl(
                f"{fx.base}/{_FX}?marker=t07-seq-b-match", proxy, extra=_STATUS_ONLY
            )
            assert r.stdout == "200"
            flow_b = await b.wait_flow("t07-seq-b-match")
            finding_b = await b.wait_finding(flow_b["id"])

            assert b.findings_for(quiet["id"]) == []
            assert b.findings_for(flow_a["id"]) == [finding_a]

        jsonl = (data_dir / "flows" / "flows.jsonl").read_text()
        assert "t07-seq-a" in jsonl
        assert "t07-seq-b-match" in jsonl
        assert _findings_index(data_dir) == [finding_a, finding_b]


async def test_concurrent_sessions_observe_their_own_flows_only(
    odda_session, odda_subprocess_session
) -> None:
    async with fixture_site([_FX]) as fx, odda_session() as a:
        await _seed_slot(a, "disclosure")
        async with odda_subprocess_session() as b:
            proxy_a = await a.call("proxy_url", {})
            proxy_b = await b.call("proxy_url", {})
            assert proxy_a != proxy_b

            for marker, proxy in (
                ("t07-cc-a1", proxy_a),
                ("t07-cc-b1", proxy_b),
                ("t07-cc-a2", proxy_a),
            ):
                r = await curl(
                    f"{fx.base}/{_FX}?marker={marker}", proxy, extra=_STATUS_ONLY
                )
                assert r.stdout == "200"

            flow_a1 = await a.wait_flow("t07-cc-a1")
            flow_a2 = await a.wait_flow("t07-cc-a2")
            flow_b1 = await b.wait_flow("t07-cc-b1")
            assert len({flow_a1["id"], flow_a2["id"], flow_b1["id"]}) == 3

            finding_a1 = await a.wait_finding(flow_a1["id"])
            finding_a2 = await a.wait_finding(flow_a2["id"])
            finding_b1 = await b.wait_finding(flow_b1["id"])

            index = _findings_index(a.data_dir)
            assert len(index) == 3
            assert len({(rec["rule_id"], rec["flow_id"]) for rec in index}) == 3
            for finding, flow_id in (
                (finding_a1, flow_a1["id"]),
                (finding_a2, flow_a2["id"]),
                (finding_b1, flow_b1["id"]),
            ):
                assert finding["rule_id"] == "disclosure.stack-trace"
                assert finding["flow_id"] == flow_id
                assert finding["severity"] == "high"

        r = await curl(f"{fx.base}/{_FX}?marker=t07-cc-a3", proxy_a, extra=_STATUS_ONLY)
        assert r.stdout == "200"
        flow_a3 = await a.wait_flow("t07-cc-a3")
        finding_a3 = await a.wait_finding(flow_a3["id"])
        assert finding_a3["flow_id"] == flow_a3["id"]
        index = _findings_index(a.data_dir)
        assert len(index) == 4
        assert len({(rec["rule_id"], rec["flow_id"]) for rec in index}) == 4


_HOLD_S = 0.3


class _SlowRule(Rule):
    def scan(self, view: FlowView) -> list[Finding]:
        time.sleep(_HOLD_S)
        return [
            Finding(
                rule_id=self.id,
                flow_id=str(view.record.get("id", "?")),
                severity=self.severity,
                title=self.title,
                evidence=(Evidence(where="response_body", offset=0, detail="slow"),),
            )
        ]


async def test_drain_keeps_loop_live_and_ordered_during_slow_scans(
    tmp_path,
) -> None:
    data_dir = tmp_path / ".odda"
    scanner = Scanner(
        data_dir,
        rules=(
            _SlowRule(id="test.slow-scan", severity=Severity.INFO, title="Slow scan"),
        ),
    )
    drain = asyncio.create_task(scanner.run())

    beats: list[float] = []

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(0.005)
            beats.append(time.monotonic())

    hb = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)

    scanner.observe(_stored_flow(data_dir, "00001"))
    scanner.observe(_stored_flow(data_dir, "00002"))

    findings_path = data_dir / "scanner" / "findings.jsonl"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if findings_path.is_file() and len(findings_path.read_text().splitlines()) == 2:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)

    hb.cancel()
    drain.cancel()
    await asyncio.gather(hb, drain, return_exceptions=True)

    index = _findings_index(data_dir)
    assert [rec["flow_id"] for rec in index] == ["00001", "00002"]
    assert all(rec["rule_id"] == "test.slow-scan" for rec in index)

    gaps = [b - a for a, b in itertools.pairwise(beats)]
    assert gaps, "heartbeat never beat"
    assert max(gaps) < _HOLD_S / 2, f"loop stalled {max(gaps):.3f}s during drain"


def test_literal_gate_never_suppresses_a_match() -> None:
    patterns: list[re.Pattern[str]] = []
    for library in _slot_module("retirejs")["_libraries"]():
        patterns.extend(library.filecontent)
    for rule in _all_slot_rules():
        patterns.extend(rule.body_patterns)
        patterns.extend(rule.header_patterns)

    gated = [p for p in patterns if _literal_gates(p.pattern, p.flags) is not None]
    assert len(gated) >= len(patterns) * 3 // 4, (
        f"gate coverage collapsed: {len(gated)}/{len(patterns)}"
    )

    rng = random.Random(20261007)
    alphabet = list(
        "ab(){}[]<>=:./*_- \n\"'0159azAZAPIkeyhttpsrcİıſKẞß"  # noqa: RUF001
    )
    texts = [
        'x mustache.version = "2.5.0"',
        "/*! jQuery v3.1.1 */",
        "/* PDFObject v2.1.1 */",
        "Traceback (most recent call last): boom",
        "AKIAIOSFODNN7EXAMPLE",
        'api_key = "sk_live_abcdefghijklmnop"',
        "https://cdn.polyfill.io/v3/polyfill.min.js",
        "<Error><Code>NoSuchBucket</Code></Error>",
        "// PDFObject v1.2.3",
        "telerik.web.ui.webresource.axd?version=2017.2.503",
        "TeLeRiK.WEB.UI.WebResource.axd ... VeRsIoN=2013.2.717",
        '<ScRiPt defer src="https://cdn.polyfill.io/v3/x.js"></script>',
        "<CoDe>  NoSuchBucket </CoDe> .. <BucketName>dangling",
    ]
    for _ in range(2500):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(5, 90)))
        if rng.random() < 0.6:
            fragments = re.findall(
                r"[A-Za-z0-9_.:/\-]{3,}", patterns[rng.randrange(len(patterns))].pattern
            )
            if fragments:
                cut = rng.randrange(len(text))
                text = text[:cut] + rng.choice(fragments) + text[cut:]
        texts.append(text)

    for text in texts:
        for pattern in patterns:
            if pattern.search(text):
                assert passes_literal_gates(pattern, text), (
                    f"gate suppressed a match: {pattern.pattern!r} vs {text!r}"
                )


def test_ci_gate_fold_sweep_pins_the_soundness_edge() -> None:
    from odda.scanner.gates import _gate_fold

    for code in list(range(0x600)) + list(range(0x1E00, 0x2200)):
        char = chr(code)
        folded = _gate_fold(char)
        if folded == char and char.lower() == char and char.upper() == char:
            continue
        for letter in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ":
            if re.fullmatch(f"(?i){letter}", char):
                assert letter.casefold() == folded, (hex(code), letter)


async def test_multi_mb_js_scan_keeps_event_loop_live(tmp_path) -> None:
    data_dir = tmp_path / ".odda"
    chunk = "var a=1;function f(){return a+1;}\n"
    big = _stored_flow(
        data_dir,
        "bigjs01",
        (chunk * (10 * 1024 * 1024 // len(chunk))).encode(),
        content_type="application/javascript",
    )
    small = _stored_flow(
        data_dir,
        "trace02",
        b'Traceback (most recent call last):\n  File "app.py", line 1\nboom\n',
    )

    scanner = Scanner(data_dir, rules=_all_slot_rules())
    drain = asyncio.create_task(scanner.run())

    beats: list[tuple[float, float]] = []

    async def heartbeat() -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.005)
            now = time.monotonic()
            beats.append((now, now - last))
            last = now

    hb = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)

    window_start = time.monotonic()
    scanner.observe(big)
    scanner.observe(small)
    findings_path = data_dir / "scanner" / "findings.jsonl"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if findings_path.is_file() and findings_path.read_text().splitlines():
            break
        await asyncio.sleep(0.005)
    window_end = time.monotonic()
    await asyncio.sleep(0.05)

    hb.cancel()
    drain.cancel()
    await asyncio.gather(hb, drain, return_exceptions=True)

    index = _findings_index(data_dir)
    assert [rec["rule_id"] for rec in index] == ["disclosure.stack-trace"]
    assert index[0]["flow_id"] == "trace02"

    gaps = [gap for ts, gap in beats if window_start <= ts <= window_end + 0.001]
    assert gaps, "heartbeat never beat during the scan window"
    ceiling = 1.5 if os.environ.get("PYTEST_XDIST_WORKER") else 0.6
    assert max(gaps) < ceiling, f"loop stalled {max(gaps):.3f}s during the drain"


async def test_multi_mb_engine_body_scan_keeps_loop_at_millisecond_cadence(
    tmp_path,
) -> None:
    data_dir = tmp_path / ".odda"
    chunk = "var a=1;function f(){return a+1;}\n"
    big = _stored_flow(
        data_dir,
        "bigjs01",
        (chunk * (10 * 1024 * 1024 // len(chunk))).encode(),
        content_type="application/javascript",
    )
    small = _stored_flow(
        data_dir,
        "trace02",
        b'Traceback (most recent call last):\n  File "app.py", line 1\nboom\n',
    )

    declarative = [r for r in _all_slot_rules() if type(r).scan is Rule.scan]
    assert len(declarative) < len(_all_slot_rules()), "dataset override must exist"
    scanner = Scanner(data_dir, rules=declarative)
    drain = asyncio.create_task(scanner.run())

    beats: list[tuple[float, float]] = []

    async def heartbeat() -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.002)
            now = time.monotonic()
            beats.append((now, now - last))
            last = now

    hb = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)

    window_start = time.monotonic()
    scanner.observe(big)
    scanner.observe(small)
    findings_path = data_dir / "scanner" / "findings.jsonl"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if findings_path.is_file() and findings_path.read_text().splitlines():
            break
        await asyncio.sleep(0.002)
    window_end = time.monotonic()
    await asyncio.sleep(0.05)

    hb.cancel()
    drain.cancel()
    await asyncio.gather(hb, drain, return_exceptions=True)

    index = _findings_index(data_dir)
    assert [rec["rule_id"] for rec in index] == ["disclosure.stack-trace"]
    assert index[0]["flow_id"] == "trace02"

    gaps = [gap for ts, gap in beats if window_start <= ts <= window_end + 0.001]
    assert gaps, "heartbeat never beat during the scan window"
    ceiling = 0.5 if os.environ.get("PYTEST_XDIST_WORKER") else 0.015
    assert max(gaps) < ceiling, f"loop stalled {max(gaps):.3f}s during the drain"


async def test_mid_session_rule_install_keeps_loop_live(
    odda_session,
) -> None:
    async with odda_session() as h:
        beats: list[tuple[float, float]] = []

        async def heartbeat() -> None:
            last = time.monotonic()
            while True:
                await asyncio.sleep(0.005)
                now = time.monotonic()
                beats.append((now, now - last))
                last = now

        hb = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.05)

        window_start = time.monotonic()
        installed = await h.call(
            "rule_install",
            {"name": "retirejs", "file": str(_RULES_SRC / "retirejs" / "rule.py")},
        )
        window_end = time.monotonic()
        await asyncio.sleep(0.05)

        hb.cancel()
        await asyncio.gather(hb, return_exceptions=True)

        assert installed["rules"] == ["js.outdated-library"]
        listing = await h.call("rule_list", {})
        assert any(row["id"] == "js.outdated-library" for row in listing)

        gaps = [gap for ts, gap in beats if window_start <= ts <= window_end + 0.001]
        assert gaps, "heartbeat never beat during the install window"
        ceiling = 0.5 if os.environ.get("PYTEST_XDIST_WORKER") else 0.15
        assert max(gaps) < ceiling, f"loop stalled {max(gaps):.3f}s during install"


_T12_MARKER = "ticket12-engine-canary"


async def test_engine_body_stage_records_match_stdlib_finditer(
    tmp_path,
) -> None:
    rules = _all_slot_rules()
    body = (
        "Traceback (most recent call last):\n"
        '  File "/app/a.py", line 1, in <module>\n'
        "Traceback (most recent call last):\n"
        "#0 /var/www/html/index.php(23): handler()\n"
        'api_key = "sk_live_abcdefghijklmnop"\n'
        "Sorry, this shop is currently unavailable\n"
        "# HELP prometheus_build_info 1\n"
        "# TYPE prometheus_build_info gauge\n"
        "<Code>NoSuchBucket</Code> filler text <BucketName>dangling-bucket\n"
    )
    data_dir = tmp_path / ".odda"
    scanner = Scanner(data_dir, rules=rules)
    scanner.observe(
        _stored_flow(data_dir, "00001", body.encode(), content_type="text/html")
    )
    drain = asyncio.create_task(scanner.run())
    findings_path = data_dir / "scanner" / "findings.jsonl"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if findings_path.is_file() and len(findings_path.read_text().splitlines()) >= 5:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    drain.cancel()
    await asyncio.gather(drain, return_exceptions=True)

    expected: dict[str, dict] = {}
    for rule in rules:
        evidence = [
            {"where": "response_body", "offset": m.start(), "detail": m.group(0)[:200]}
            for pattern in rule.body_patterns
            for m in pattern.finditer(body)
        ]
        if evidence:
            expected[rule.id] = {
                "rule_id": rule.id,
                "flow_id": "00001",
                "severity": rule.severity.value,
                "title": rule.title,
                "evidence": evidence,
                "refs": list(rule.refs),
            }
    assert set(expected) == {
        "disclosure.stack-trace",
        "disclosure.frontend-secret",
        "takeover.s3-nosuchbucket",
        "takeover.shopify",
        "exposure.prometheus-metrics",
    }

    index = _findings_index(data_dir)
    assert {rec["rule_id"] for rec in index} == set(expected)
    for rec in index:
        assert rec == expected[rec["rule_id"]], rec["rule_id"]


async def test_arrival_epoch_never_matches_retroactively(tmp_path) -> None:
    data_dir = tmp_path / ".odda"
    body = f"pre {_T12_MARKER}-7 mid post\n"
    canary = Rule(
        id="user.ticket12-canary",
        severity=Severity.LOW,
        title="Ticket 12 canary marker disclosed",
        body_patterns=(rf"{_T12_MARKER}-[0-9]+",),
    )
    scanner = Scanner(data_dir)
    pre = _stored_flow(data_dir, "00001", body.encode())
    scanner.observe(pre)
    scanner.register_rules((canary,))
    live = _stored_flow(data_dir, "00002", body.encode())
    scanner.observe(live)
    scanner.deregister_rules({"user.ticket12-canary"})
    post = _stored_flow(data_dir, "00003", body.encode())
    scanner.observe(post)
    drain = asyncio.create_task(scanner.run())
    findings_path = data_dir / "scanner" / "findings.jsonl"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if findings_path.is_file() and findings_path.read_text().splitlines():
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.2)
    drain.cancel()
    await asyncio.gather(drain, return_exceptions=True)

    index = _findings_index(data_dir)
    assert [(rec["rule_id"], rec["flow_id"]) for rec in index] == [
        ("user.ticket12-canary", "00002")
    ]
    assert index[0]["evidence"][0]["detail"] == f"{_T12_MARKER}-7"


_T13_HIT_BODY = (
    "# HELP go_goroutines Number of goroutines\n"
    "# TYPE go_goroutines gauge\n"
    'config = dict(api_key = "sk_live_abcdefghijklmnop")\n'
    '<script defer src="https://cdn.bootcdn.net.example.com/x.js"></script>\n'
    '<script defer src="https://cdn.bootcss.com/x.js"></script>\n'
)

# See ADR-0037.
_T13_REFUSED = Rule(
    id="user.ticket13-refused",
    severity=Severity.LOW,
    title="Ticket 13 refusal families",
    body_patterns=(
        r"(?m)^# HELP ((?:prometheus|go|process)_[a-zA-Z0-9_:]+)[^\r\n]*\r?\n"
        r"# TYPE \1 (?:counter|gauge|histogram|summary)\b",
        r"\b(?:api[_-]?key|secret|token|password)\s*[:=]\s*"
        r"([\"'])[A-Za-z0-9+/=_.\-]{4,32}\1",
        r"""(?i)<script\b[^>]{0,40}\ssrc\s*=\s*["'][^"']*(?<![\w-])"""
        r"""(?:polyfill\.io|bootcdn\.net|bootcss\.com)(?![\w.-])[^"']*["']""",
    ),
)


async def test_confirmation_runs_only_on_approximation_hit_bodies(
    tmp_path, monkeypatch
) -> None:
    from odda.scanner import engine, fastscan

    confirms: list[tuple[str, str]] = []
    residuals: list[str] = []
    real_confirm = fastscan._confirmed_spans  # noqa: SLF001 — the spy seam
    real_evidence = engine._pattern_evidence  # noqa: SLF001 — the spy seam

    def counting_confirm(pattern: re.Pattern, text: str) -> list:
        confirms.append((pattern.pattern, text))
        return real_confirm(pattern, text)

    def counting_evidence(patterns: tuple, text: str, where: str) -> list:
        if where == "response_body":
            residuals.append(patterns[0].pattern)
        return real_evidence(patterns, text, where)

    monkeypatch.setattr(fastscan, "_confirmed_spans", counting_confirm)
    monkeypatch.setattr(engine, "_pattern_evidence", counting_evidence)

    data_dir = tmp_path / ".odda"
    scanner = Scanner(data_dir, rules=(_T13_REFUSED,))
    miss = _stored_flow(
        data_dir, "miss01", b"plain ascii payload with nothing exotic in it\n"
    )
    hit = _stored_flow(data_dir, "hit02", _T13_HIT_BODY.encode())
    scanner.observe(miss)
    scanner.observe(hit)
    drain = asyncio.create_task(scanner.run())
    findings_path = data_dir / "scanner" / "findings.jsonl"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if findings_path.is_file() and findings_path.read_text().splitlines():
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.15)
    drain.cancel()
    await asyncio.gather(drain, return_exceptions=True)

    expected_evidence = [
        {
            "where": "response_body",
            "offset": match.start(),
            "detail": match.group(0)[:200],
        }
        for pattern in _T13_REFUSED.body_patterns
        for match in pattern.finditer(_T13_HIT_BODY)
    ]
    index = _findings_index(data_dir)
    assert len(index) == 1
    assert index[0] == {
        "rule_id": "user.ticket13-refused",
        "flow_id": "hit02",
        "severity": "low",
        "title": "Ticket 13 refusal families",
        "evidence": expected_evidence,
        "refs": [],
    }

    assert all("nothing exotic" not in text for _src, text in confirms)
    assert len(confirms) == 3, [src for src, _ in confirms]
    assert {src for src, _ in confirms} == {
        pattern.pattern for pattern in _T13_REFUSED.body_patterns
    }
    assert residuals == []


async def test_confirmation_over_multi_mb_body_keeps_residual_ceiling(
    tmp_path,
) -> None:
    data_dir = tmp_path / ".odda"
    noise = "var a=1;function f(){return a+1;}\n"
    unit = (
        noise * 96
        + "# HELP go_noise metric documentation line\n"
        + "# TYPE other_family counter\n"
    )
    blocks = []
    count = 10 * 1024 * 1024 // len(unit)
    for i in range(count):
        blocks.append(unit)
        if i % 32 == 31:
            blocks.append(
                '<script defer src="https://cdn.bootcdn.net.example.com/x.js">'
                "</script>\n"
            )
    big_body = (
        "".join(blocks)
        + "# HELP process_open_fds FD usage\n"
        + "# TYPE process_open_fds gauge\n"
        + '<script defer src="https://cdn.bootcss.com/x.js"></script>\n'
    )
    small_body = (
        "# HELP prometheus_tsdb_head_series Total head series\n"
        "# TYPE prometheus_tsdb_head_series gauge\n"
    )
    big = _stored_flow(
        data_dir, "bigjs01", big_body.encode(), content_type="text/plain"
    )
    small = _stored_flow(
        data_dir, "prom02", small_body.encode(), content_type="text/plain"
    )
    scanner = Scanner(
        data_dir, rules=(*_slot_rules("supplychain"), *_slot_rules("fingerprints"))
    )
    drain = asyncio.create_task(scanner.run())

    beats: list[tuple[float, float]] = []

    async def heartbeat() -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.005)
            now = time.monotonic()
            beats.append((now, now - last))
            last = now

    hb = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)

    window_start = time.monotonic()
    scanner.observe(big)
    scanner.observe(small)
    findings_path = data_dir / "scanner" / "findings.jsonl"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if findings_path.is_file() and len(findings_path.read_text().splitlines()) >= 3:
            break
        await asyncio.sleep(0.005)
    window_end = time.monotonic()
    await asyncio.sleep(0.05)

    hb.cancel()
    drain.cancel()
    await asyncio.gather(hb, drain, return_exceptions=True)

    index = _findings_index(data_dir)
    assert [(rec["rule_id"], rec["flow_id"]) for rec in index] == [
        ("js.malicious-cdn-import", "bigjs01"),
        ("exposure.prometheus-metrics", "bigjs01"),
        ("exposure.prometheus-metrics", "prom02"),
    ]
    assert index[0]["evidence"][0]["detail"].startswith("<script")
    assert index[1]["evidence"][0]["detail"].startswith("# HELP process_open_fds")
    assert index[2]["evidence"][0]["detail"].startswith("# HELP prometheus_tsdb")

    gaps = [gap for ts, gap in beats if window_start <= ts <= window_end + 0.001]
    assert gaps, "heartbeat never beat during the scan window"
    ceiling = 1.5 if os.environ.get("PYTEST_XDIST_WORKER") else 0.6
    assert max(gaps) < ceiling, f"loop stalled {max(gaps):.3f}s during the drain"


def test_config_home_resolver_honors_override_and_keeps_default_paths(
    tmp_path, monkeypatch
) -> None:
    from odda import paths

    monkeypatch.delenv(paths.CONFIG_HOME_ENV, raising=False)
    home = Path.home() / ".config" / "odda"
    assert paths.config_home() == home
    assert paths.base_profile_dir() == home / "chrome-profile"
    assert paths.global_rules_dir() == home / "rules"

    override = tmp_path / "t15-redirected-home"
    monkeypatch.setenv(paths.CONFIG_HOME_ENV, str(override))
    assert paths.config_home() == override
    assert paths.base_profile_dir() == override / "chrome-profile"
    assert paths.global_rules_dir() == override / "rules"


async def test_rule_slots_are_project_scoped_with_a_shared_global_tree(
    odda_subprocess_session, config_home, tmp_path
) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()

    async with fixture_site(index_body=_T06_BODY) as fx:
        async with odda_subprocess_session(cwd=project_a) as a:
            pre = await _drive(a, fx, "t15-pre-install")
            r = await a.call("rule_install", {"name": "local", "source": _RULE_CANARY})
            assert r["rules"] == ["user.ticket06-canary"]
            assert (project_a / ".odda" / "rules" / "local" / "rule.py").is_file()
            assert not (config_home / "rules" / "local").exists()

            post = await _drive(a, fx, "t15-post-install")
            await _wait_rule_finding(a, post["id"], "user.ticket06-canary")
            assert all(
                f["rule_id"] != "user.ticket06-canary"
                for f in a.findings_for(pre["id"])
            )

        curated = config_home / "rules" / "curated"
        curated.mkdir(parents=True)
        (curated / "rule.py").write_text(_RULE_ALT, encoding="utf-8")

        async with odda_subprocess_session(cwd=project_b) as b:
            rows = await b.call("rule_list", {})
            by_id = {row["id"]: row for row in rows}
            assert "user.ticket06-canary" not in by_id
            assert not (project_b / ".odda" / "rules").exists()
            assert by_id["user.ticket06-alt"] == {
                "slot": "curated",
                "id": "user.ticket06-alt",
                "severity": "low",
                "scope": "global",
                "state": "active",
                "error": None,
            }

            flow = await _drive(b, fx, "t15-cross-boot")
            finding = await _wait_rule_finding(b, flow["id"], "user.ticket06-alt")
            assert finding["flow_id"] == flow["id"]


async def test_rule_guide_resource_covers_the_authoring_surface(
    odda_session,
) -> None:
    async with odda_session() as h:
        listed = await h.client.list_resources()
        guide = next(
            (r for r in listed.resources if str(r.uri) == "odda://scanner/rule-guide"),
            None,
        )
        assert guide is not None, "rule guide not listed over MCP"

        r = await h.client.read_resource("odda://scanner/rule-guide")
        text = r.contents[0].text

        for heading in (
            "## Installing rules — arrival-time semantics",
            "## The rule file",
            "## Rule fields — matchers and gates",
            "## The severity scale",
            "## Rule ids are permanent",
            "## scan() overrides — dataset-style rules",
            "## FlowView — what a scan() override can read",
            "## Worked example — declarative rule",
            "## Worked example — scan() override",
            "## Engine compatibility — the pattern-shape tradeoff",
        ):
            assert heading in text, f"guide section missing: {heading}"

        assert RULE_FILE_SKELETON in text

        for field in (
            "body_patterns",
            "header_patterns",
            "request_query_patterns",
            "request_header_patterns",
            "schemes",
            "statuses",
            "content_types",
            "browser_only",
            "request_browser_only",
        ):
            assert field in text, f"rule field not documented: {field}"

        for attr in (
            "record",
            "request",
            "request_method",
            "request_path",
            "request_query",
            "request_headers",
            "response_headers",
            "body",
            "body_bytes",
            "content_type",
            "is_browser",
        ):
            assert attr in text, f"FlowView attribute not documented: {attr}"

        for level in ("info", "low", "medium", "high", "critical"):
            assert level in text

        assert "body_patterns=(" in text
        assert "def scan(self, view" in text

        assert "prefilter" in text
        assert "confirm" in text
        assert "backtrack" in text


async def test_rule_install_description_is_contract_plus_guide_pointer(
    odda_session,
) -> None:
    async with odda_session() as h:
        tools = await h.client.list_tools()
        doc = next(t.description for t in tools.tools if t.name == "rule_install")
        assert "odda://scanner/rule-guide" in doc
        assert "RULES = [" not in doc
        assert "from odda.scanner import" not in doc
        assert "never retroactively" in doc
        assert "assets" in doc
