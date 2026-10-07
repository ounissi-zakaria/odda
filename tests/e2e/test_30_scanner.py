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
from urllib.parse import quote

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
        "shutil",  # slot removal (rmtree) — filesystem only, no sockets
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


# --- ticket 02: disclosure builtin family ---

_PK = "private-key.html"
_PC = "public-cert.html"


async def test_private_key_block_lands_finding(odda_session) -> None:
    """A PEM private-key block in a response body yields a
    disclosure.private-key Finding (HIGH) with body evidence and refs."""
    async with odda_session() as h, fixture_site([_PK]) as fx:
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
        # The fixture's match inventory: the RSA and the OpenSSH BEGIN
        # sentinels — two blocks, one finding.
        assert len(finding["evidence"]) == 2
        details = {e["detail"] for e in finding["evidence"]}
        assert any("RSA PRIVATE KEY" in d for d in details)
        assert any("OPENSSH PRIVATE KEY" in d for d in details)
        assert all(e["where"] == "response_body" for e in finding["evidence"])
        assert len({e["offset"] for e in finding["evidence"]}) == 2
        assert len(h.findings_for(flow["id"])) == 1


async def test_public_certificate_block_does_not_fire(odda_session) -> None:
    """A public-key / certificate PEM block is not a private key — no
    finding for published trust material."""
    async with odda_session() as h, fixture_site([_PC]) as fx:
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
    """A hardcoded secret assignment in a served JS body yields a
    disclosure.frontend-secret Finding (MEDIUM) with the assignments and
    the logger line as evidence."""
    async with odda_session() as h, fixture_site([_FS]) as fx:
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
        # The fixture's match inventory: client_secret, refreshToken,
        # and api_key assignments, a quoted "apiKey" property, and two
        # password-leaking logger lines — six matches, one finding.
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
    """Ordinary JS property names that merely contain "key" (hotkey,
    keyword, apiKeyId, keydown) are not secrets — no finding, and the
    scanner's subtree stays absent."""
    async with odda_session() as h, fixture_site([_BK]) as fx:
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
    """The frontend-secret gate holds for 2xx responses only: the same
    assignment shape served with status 404 yields no finding."""
    async with odda_session() as h, dyn_server() as dyn:
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
    """An AWS access key id in a response body yields a
    disclosure.aws-key Finding (HIGH); the near-miss decoys in the same
    body (truncated, lowercase, embedded) do not add evidence."""
    async with odda_session() as h, fixture_site([_AWB]) as fx:
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
    """An AWS access key id in a response header yields one
    disclosure.aws-key Finding whose evidence is located in the stored
    response headers (where="response_headers")."""
    async with odda_session() as h, dyn_server() as dyn:
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
        # Header side only: the body is empty, so the single evidence
        # entry comes from the stored response headers.
        assert len(finding["evidence"]) == 1
        first = finding["evidence"][0]
        assert first["where"] == "response_headers"
        assert first["detail"] == "AKIAIOSFODNN7EXAMPLE"
        assert isinstance(first["offset"], int)
        assert len(h.findings_for(flow["id"])) == 1


async def test_xdebug_setcookie_lands_finding(odda_session) -> None:
    """A Set-Cookie: XDEBUG_SESSION= response yields a
    cookie.xdebug-session Finding (HIGH) whose refs point at the Xdebug
    remote-debug documentation, evidence located in the response
    headers."""
    async with odda_session() as h, dyn_server() as dyn:
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
    """A Set-Cookie response for an ordinary session cookie is not the
    Xdebug echo — no finding."""
    async with odda_session() as h, dyn_server() as dyn:
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


# --- ticket 06: user rules + rule tool trio ---

_T06_CANARY = "odda-ticket06-canary"
# One body drives both families: the canary marker for the user rules
# under test, and a Python traceback banner so the builtin
# disclosure.stack-trace finding doubles as the drain-settle probe for
# negative assertions on later flows.
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
        title="Collides with a builtin id",
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

# Two rules in one slot: a normal matcher plus a rule whose scheme gate
# raises at scan time (a str subclass whose __eq__ raises inside the
# engine's ``in`` gate check) — the deterministic raising-rule
# injection ticket 01 deferred. Containment must be per-rule, so the
# sibling (and the builtin) still fire on the same flow.
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
    """Bounded poll for one flow's finding from one specific rule."""
    for _ in range(200):
        for f in h.findings_for(flow_id):
            if f["rule_id"] == rule_id:
                return f
        await asyncio.sleep(0.05)
    raise AssertionError(f"no {rule_id!r} finding for flow {flow_id!r}")


async def test_rule_install_applies_to_flows_after_install_only(
    odda_session,
) -> None:
    """Installing from source mid-session: matching traffic driven after
    the install yields the rule's Finding; traffic that flowed before the
    install does not retroactively match, even though its scan drains
    after the install."""
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

        # The pre-install flow never matches the rule (rules apply from
        # arrival, not from scan time); settled by the post flow's
        # canary finding — the drain passed the pre flow's entry.
        assert all(
            f["rule_id"] != "user.ticket06-canary" for f in h.findings_for(pre["id"])
        )


async def test_rule_install_from_file_and_rule_list_rows(
    odda_session, tmp_path
) -> None:
    """install from a file path persists the slot; rule_list shows builtin
    and user rows with slot, id, severity, source, and state."""
    rf = tmp_path / "t06_canary_rule.py"
    rf.write_text(_RULE_CANARY)
    async with odda_session() as h:
        r = await h.call("rule_install", {"name": "alpha", "file": str(rf)})
        assert r["name"] == "alpha"
        assert r["size"] > 0
        assert "user.ticket06-canary" in _slot_path(h, "alpha").read_text()

        rows = await h.call_json("rule_list", {})
        by_id = {row["id"]: row for row in rows}
        assert by_id["disclosure.stack-trace"] == {
            "slot": None,
            "id": "disclosure.stack-trace",
            "severity": "high",
            "source": "builtin",
            "state": "active",
            "error": None,
        }
        assert by_id["user.ticket06-canary"] == {
            "slot": "alpha",
            "id": "user.ticket06-canary",
            "severity": "medium",
            "source": "user",
            "state": "active",
            "error": None,
        }


async def test_syntax_error_install_rejected_with_expected_shape(
    odda_session,
) -> None:
    """A syntax-error source is rejected naming the problem and showing
    the expected rule-file shape; nothing is persisted."""
    async with odda_session() as h:
        err = await h.call_error(
            "rule_install", {"name": "bad", "source": _RULE_BROKEN}
        )
        assert "Failed to load rule slot 'bad'" in err
        assert "SyntaxError" in err
        # the expected file shape rides the rejection
        assert "from odda.scanner import Rule, Severity" in err
        assert "RULES = [" in err
        assert not _slot_path(h, "bad").exists()


async def test_colliding_rule_ids_rejected_naming_the_collision(
    odda_session,
) -> None:
    """A rule id colliding with a builtin or another user rule's id is
    rejected at install, naming the exact collision; the colliding slot
    never reaches disk."""
    async with odda_session() as h:
        err = await h.call_error(
            "rule_install", {"name": "clash-builtin", "source": _RULE_COLLIDING}
        )
        assert "Rule id 'disclosure.stack-trace'" in err
        assert "builtin" in err
        assert not _slot_path(h, "clash-builtin").exists()

        await h.call("rule_install", {"name": "dup-a", "source": _RULE_DUP})
        err = await h.call_error("rule_install", {"name": "dup-b", "source": _RULE_DUP})
        assert "Rule id 'user.ticket06-dup'" in err
        assert "'dup-a'" in err
        assert not _slot_path(h, "dup-b").exists()


async def test_rule_remove_keeps_past_findings_stops_future(odda_session) -> None:
    """rule_remove deregisters the slot's rules live and deletes the slot;
    past Findings remain in the index; subsequent matching traffic
    produces no Finding from the removed rules."""
    async with odda_session() as h, fixture_site(index_body=_T06_BODY) as fx:
        await h.call("rule_install", {"name": "canary", "source": _RULE_CANARY})
        before = await _drive(h, fx, "t06-remove-before")
        await _wait_rule_finding(h, before["id"], "user.ticket06-canary")

        r = await h.call("rule_remove", {"name": "canary"})
        assert r == {
            "name": "canary",
            "removed": True,
            "rules": ["user.ticket06-canary"],
        }
        assert not _slot_path(h, "canary").parent.exists()

        after = await _drive(h, fx, "t06-remove-after")
        # settle: this flow drained (the builtin fired on it), so the
        # canary absence below is the removal, not drain lag
        await _wait_rule_finding(h, after["id"], "disclosure.stack-trace")
        assert all(
            f["rule_id"] != "user.ticket06-canary" for f in h.findings_for(after["id"])
        )
        # past findings are never retracted
        assert any(
            f["rule_id"] == "user.ticket06-canary" for f in h.findings_for(before["id"])
        )


async def test_raising_rule_is_contained_per_rule(odda_session) -> None:
    """A user rule that raises at scan time is logged and skipped: the
    flow still lands fully in the flows index, and other rules — the
    builtin and a sibling user rule from the same slot — still fire on
    that flow."""
    async with odda_session() as h, fixture_site(index_body=_T06_BODY) as fx:
        r = await h.call("rule_install", {"name": "pack", "source": _RULE_PACK})
        assert r["rules"] == ["user.ticket06-sibling", "user.ticket06-raiser"]

        flow = await _drive(h, fx, "t06-raiser-boom")
        # the flow landed fully: request, response headers, and body files
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
    """file/source exclusivity, missing file, empty source, remove-missing,
    and slot-name collision all error verbatim (the proxy-script
    install contract)."""
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
    """force=True overwrites an existing slot: the old rule ids leave the
    live set, the on-disk source is the new one, and subsequent traffic
    finds only the replacement rule."""
    async with odda_session() as h, fixture_site(index_body=_T06_BODY) as fx:
        await h.call("rule_install", {"name": "swap", "source": _RULE_CANARY})
        r = await h.call(
            "rule_install", {"name": "swap", "source": _RULE_ALT, "force": True}
        )
        assert r["rules"] == ["user.ticket06-alt"]
        assert "user.ticket06-alt" in _slot_path(h, "swap").read_text()

        rows = await h.call_json("rule_list", {})
        ids = {row["id"] for row in rows}
        assert "user.ticket06-alt" in ids
        assert "user.ticket06-canary" not in ids

        flow = await _drive(h, fx, "t06-force-swap")
        await _wait_rule_finding(h, flow["id"], "user.ticket06-alt")
        assert all(
            f["rule_id"] != "user.ticket06-canary" for f in h.findings_for(flow["id"])
        )


async def test_slots_persist_and_load_on_next_boot_bad_slot_skipped(
    odda_session, tmp_path
) -> None:
    """A slot installed in session A loads on session B's boot; a broken
    slot (syntax error on disk) is skipped with its error visible in
    rule_list while the good sibling slot still loads and fires."""
    async with odda_session() as h:
        await h.call("rule_install", {"name": "keeper", "source": _RULE_CANARY})
        assert _slot_path(h, "keeper").is_file()

    # a broken slot appears on disk out-of-band (hand-edited, corrupted)
    broken = tmp_path / ".odda" / "rules" / "broken" / "rule.py"
    broken.parent.mkdir(parents=True)
    broken.write_text(_RULE_BROKEN)

    async with odda_session() as h, fixture_site(index_body=_T06_BODY) as fx:
        rows = await h.call_json("rule_list", {})
        failed = [row for row in rows if row["state"] == "failed"]
        assert len(failed) == 1
        assert failed[0]["slot"] == "broken"
        assert failed[0]["source"] == "user"
        assert "SyntaxError" in failed[0]["error"]

        keeper = next(row for row in rows if row["id"] == "user.ticket06-canary")
        assert keeper == {
            "slot": "keeper",
            "id": "user.ticket06-canary",
            "severity": "medium",
            "source": "user",
            "state": "active",
            "error": None,
        }

        flow = await _drive(h, fx, "t06-boot-restored")
        finding = await _wait_rule_finding(h, flow["id"], "user.ticket06-canary")
        assert finding["flow_id"] == flow["id"]
