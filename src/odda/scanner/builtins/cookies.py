"""Cookie-family builtin rules (ADR-0035's signal bar).

The Xdebug session echo is an RCE candidate: a server that answers
with ``Set-Cookie: XDEBUG_SESSION=`` is running the Xdebug extension
with remote debugging reachable, and a debug client that completes
the handshake can evaluate arbitrary PHP. The cookie echo is the
passive tell — rare, specific, and directly actionable.

The response-side matcher lives in ``header_patterns``. The rule is
deliberately the single home for the check: the request-param half
(the ``?XDEBUG_SESSION=`` start-request, gated to browser-attributed
flows so crafted probes never trip it) joins this same rule as a
request-side matcher alongside the header one, without touching the
family composition.
"""

from __future__ import annotations

from odda.scanner.model import Rule, Severity

RULES: tuple[Rule, ...] = (
    Rule(
        id="cookie.xdebug-session",
        severity=Severity.HIGH,
        title="Xdebug session cookie echoed by the server (RCE candidate)",
        header_patterns=(r"(?i:^set-cookie:\s*xdebug_session=)",),
        refs=("https://xdebug.org/docs/step_debug",),
    ),
)
