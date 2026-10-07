"""Cookie-family builtin rules (ADR-0035's signal bar).

The Xdebug session echo is an RCE candidate: a server that answers
with ``Set-Cookie: XDEBUG_SESSION=`` is running the Xdebug extension
with remote debugging reachable, and a debug client that completes
the handshake can evaluate arbitrary PHP. The same is true of the
request side — a browser navigating with ``?XDEBUG_SESSION=``
start-parameters is someone (or something) driving a debug session.

The rule is deliberately the single home for the check: the
response-side matcher (``header_patterns``, ungated — any flow may
carry the server's echo) and the request-param half
(``request_query_patterns``, gated to browser-attributed flows via
``request_browser_only`` so crafted ``request_send`` probes never
trip it) share the id, and a flow matching both halves aggregates
into one finding.
"""

from __future__ import annotations

from odda.scanner.model import Rule, Severity

RULES: tuple[Rule, ...] = (
    Rule(
        id="cookie.xdebug-session",
        severity=Severity.HIGH,
        title="Xdebug debug session observed (RCE candidate)",
        header_patterns=(r"(?i:^set-cookie:\s*xdebug_session=)",),
        request_query_patterns=(r"(?i:^xdebug_session_start$)",),
        request_browser_only=True,
        refs=("https://xdebug.org/docs/step_debug",),
    ),
)
