"""Failed CONNECTs (https to dead hosts) land as error flows.

An https subresource whose host does not resolve has no inner request:
the browser sends CONNECT, mitmproxy's connect layer fails resolution,
and the flow store writes the Flow model's error shape for the tunnel
attempt itself (status_code null, the resolution-failure message as the
error, browser attribution from the CONNECT's proxy credentials).
Successful CONNECTs still write nothing (the tunnel's inner flows carry
the traffic), cleartext-http error flows are unchanged, and benign
client cancels before tunnel establishment do not spam the store.
"""

import asyncio
import json
import socket
import time

from odda.flowstore import _connect_failure_message
from tests.e2e.conftest import curl, dyn_server, fixture_site

# The DNS-failure wording glibc produces for a non-resolving name (the
# contrast case's verbatim error, EAI_NONAME).
_DNS_FAILURE = "Name or service not known"

# A minimal request-side error-flow rule — the rule-guide skeleton style
# (browser gate + DNS-shaped error text), hermetic to this module: no
# dependency on any installed global rule.
_RULE_DANGLING = '''from odda.scanner import Evidence, Finding, Rule, Severity
import re

_DNS = re.compile(r"Name or service not known")


class DanglingHost(Rule):
    """Fire on browser-attributed error flows whose error says the
    name did not resolve."""

    def scan(self, view):
        record = view.record
        if not view.is_browser:
            return None
        error = record.get("error") or ""
        if not _DNS.search(error):
            return None
        host = record.get("host") or "?"
        return [
            Finding(
                rule_id=self.id,
                flow_id=str(record.get("id", "?")),
                severity=self.severity,
                title=self.title,
                evidence=(
                    Evidence(
                        where="request",
                        offset=0,
                        detail=f"CONNECT https://{host} failed: {error}",
                    ),
                ),
                refs=self.refs,
            )
        ]


RULES = [
    DanglingHost(
        id="user.test34-dangling",
        severity=Severity.MEDIUM,
        title="test34 dangling host over https",
    ),
]
'''


def _dead_page(marker_host: str) -> str:
    """A fixture page whose subresource target lives on a dead https host."""
    return (
        "<!doctype html><html><head>"
        f'<script src="https://{marker_host}/b.js"></script>'
        "</head><body>test34</body></html>"
    )


async def test_https_dns_failure_connect_lands_error_flow(odda_session) -> None:
    """A browser subresource on a non-resolving https host produces one
    error flow: flow dir + index line, error set, status null,
    browser-attributed, CONNECT method, https scheme."""
    host = "t34-https-dead.invalid"
    async with odda_session() as h, fixture_site(index_body=_dead_page(host)) as fx:
        bid, tid = await h.open_browser()
        await h.navigate(bid, tid, fx.base)

        flow = await h.wait_flow(host)
        assert flow["status_code"] is None
        assert _DNS_FAILURE in flow["error"]
        assert flow["browser_id"] == bid
        assert flow["method"] == "CONNECT"
        assert flow["scheme"] == "https"
        assert flow["host"] == host
        assert flow["port"] == 443

        # The Flow model's full error shape on disk: flow dir with the
        # request (authority form), meta.json sidecar, and error file.
        flow_dir = h.data_dir / "flows" / flow["id"]
        request = (flow_dir / "request").read_bytes()
        assert request.startswith(f"CONNECT {host}:443 HTTP/1.1\r\n".encode())
        meta = json.loads((flow_dir / "meta.json").read_text())
        assert meta == {"scheme": "https", "host": host, "port": 443}
        assert _DNS_FAILURE in (flow_dir / "error").read_text()

        # One error flow for the failed subresource, not one per retry.
        assert len(h.flows_with(host)) == 1


async def test_https_connect_refused_error_is_distinguishable(odda_session) -> None:
    """A refused https upstream (host resolves, port dead) lands its own
    error flow with the connect-failure wording, not the DNS wording."""
    async with odda_session() as h:
        proxy = await h.call("proxy_url", {})
        await curl(
            "https://127.0.0.1:1/x.js",
            proxy,
            extra=["-o", "/dev/null", "-k", "-w", "%{http_code}"],
        )
        # The CONNECT record's path is the authority form (host:port) —
        # the inner request whose query would carry a marker never exists.
        flow = await h.wait_flow("127.0.0.1:1")
        assert flow["status_code"] is None
        assert "Connect call failed" in flow["error"]
        assert _DNS_FAILURE not in flow["error"]
        assert flow["scheme"] == "https"


async def test_request_side_rule_fires_on_https_error_flow(odda_session) -> None:
    """A request-side error-flow rule (installed from source, hermetic)
    sees the https CONNECT error flow — the shape takeover.dangling-host
    uses in the real path."""
    host = "t34-rule-dead.invalid"
    async with odda_session() as h, fixture_site(index_body=_dead_page(host)) as fx:
        await h.call(
            "rule_install", {"name": "test34-dangling", "source": _RULE_DANGLING}
        )
        bid, tid = await h.open_browser()
        await h.navigate(bid, tid, fx.base)

        flow = await h.wait_flow(host)
        finding = await h.wait_finding(flow["id"])
        assert finding["rule_id"] == "user.test34-dangling"
        assert finding["severity"] == "medium"
        assert host in finding["evidence"][0]["detail"]


async def test_successful_connect_writes_no_extra_flow(odda_session) -> None:
    """A successful https CONNECT captures only the inner flow — no
    tunnel-shaped record rides along."""
    async with odda_session() as h, dyn_server() as dyn:
        proxy = await h.call("proxy_url", {})
        r = await curl(
            f"{dyn.tls_base}/a?body=t34-ok&status=200&marker=t34-tunnel-ok",
            proxy,
            extra=["-o", "/dev/null", "-k", "-w", "%{http_code}"],
        )
        assert r.stdout == "200"

        flow = await h.wait_flow("t34-tunnel-ok")
        assert flow["method"] == "GET"
        assert flow["status_code"] == 200
        for rec in h.flows_with(f"127.0.0.1:{dyn.port}"):
            assert rec["method"] != "CONNECT", f"tunnel record leaked: {rec}"


async def test_cleartext_http_dns_failure_unchanged(odda_session) -> None:
    """The pre-existing shape is untouched: one cleartext-http error flow
    per failed subresource, GET method, http scheme, DNS wording."""
    async with odda_session() as h:
        proxy = await h.call("proxy_url", {})
        await curl(
            "http://t34-plain-dead.invalid/a.js?marker=t34-plain",
            proxy,
            extra=["-o", "/dev/null", "-w", "%{http_code}"],
        )
        flow = await h.wait_flow("t34-plain")
        assert flow["status_code"] is None
        assert _DNS_FAILURE in flow["error"]
        assert flow["method"] == "GET"
        assert flow["scheme"] == "http"
        assert len(h.flows_with("t34-plain-dead.invalid")) == 1


async def test_benign_connect_cancel_writes_no_flow(odda_session) -> None:
    """A client that walks away while the tunnel is still pending (the
    page-closed-before-CONNECT-completes shape) leaves no flow at all."""
    async with odda_session() as h:
        proxy = await h.call("proxy_url", {})
        # 10.255.255.1 never answers — the CONNECT hangs until the
        # client closes, exactly the benign cancel.
        host = "10.255.255.1"

        def _cancel() -> None:
            addr = proxy["url"] if isinstance(proxy, dict) else str(proxy)
            port = int(addr.rsplit(":", 1)[1])
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            s.sendall(
                f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode()
            )
            time.sleep(0.4)
            s.close()

        before = h.flow_count()
        await asyncio.to_thread(_cancel)
        # Bounded settle, no early exit: an absence assertion has no
        # observable to poll for, but the disconnect must reach the
        # proxy and the (filtered) connect-error hook fire before we
        # assert absence.
        for _ in range(60):
            await asyncio.sleep(0.05)
        assert h.flows_with(host) == []
        assert h.flow_count() == before


class _Err:
    """Duck-typed stand-in for mitmproxy's Error."""

    def __init__(self, msg: str) -> None:
        self.msg = msg


class _Resp:
    """Duck-typed stand-in for mitmproxy's Response."""

    def __init__(self, body: str) -> None:
        self.content = body.encode()

    def get_text(self, *, strict: bool = False) -> str:
        del strict  # signature parity with mitmproxy's Response.get_text
        return self.content.decode()


class _Flow:
    """Duck-typed stand-in for the CONNECT flow at http_connect_error."""

    def __init__(self, error: _Err | None = None, response: _Resp | None = None):
        self.error = error
        self.response = response


# Pinning tests: the 502-body wording below is audited against
# mitmproxy 12.2.3 (the version pinned in .venv). Phrasing drift in an
# upstream upgrade must FAIL these tests so the maintainer updates
# _CONNECT_502 — the pin exists precisely because silent capture loss
# is otherwise invisible.


def test_connect_failure_message_parses_synthesized_502() -> None:
    """The verbatim 12.2.3 502 body yields the upstream error text."""
    flow = _Flow(
        response=_Resp(
            "Cannot connect to example.com:443: [Errno -2] Name or service"
            " not known If you plan to route traffic through mitmproxy,"
            " adjust the connection settings"
        )
    )
    assert _connect_failure_message(flow) == "[Errno -2] Name or service not known"


def test_connect_failure_message_parses_502_without_suffix() -> None:
    """Bodies ending right after the error (the regex's $ alternative)
    still extract."""
    flow = _Flow(
        response=_Resp(
            "Cannot connect to example.com:443: [Errno -2] Name or service not known"
        )
    )
    assert _connect_failure_message(flow) == "[Errno -2] Name or service not known"


def test_connect_failure_message_prefers_flow_error() -> None:
    """A populated flow.error wins verbatim over the 502 body."""
    flow = _Flow(error=_Err("Connect call failed"))
    assert _connect_failure_message(flow) == "Connect call failed"


def test_connect_failure_message_filters_client_cancels() -> None:
    """Benign cancels — exact and suffixed (substring match) — yield None."""
    assert _connect_failure_message(_Flow(error=_Err("client disconnected"))) is None
    assert (
        _connect_failure_message(_Flow(error=_Err("connection killed: extra suffix")))
        is None
    )


def test_connect_failure_message_no_signal_yields_none() -> None:
    """Neither an error nor a body leaves nothing to capture."""
    assert _connect_failure_message(_Flow()) is None
    assert _connect_failure_message(_Flow(response=_Resp(""))) is None


def test_connect_failure_message_unparsable_body_yields_none() -> None:
    """Phrasing drift (a body the regex cannot parse) yields None rather
    than a wrong capture — and the pins above make that drift loud."""
    assert _connect_failure_message(_Flow(response=_Resp("upstream said no"))) is None
