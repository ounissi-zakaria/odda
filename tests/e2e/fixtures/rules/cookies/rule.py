# See ADR-0035.
from __future__ import annotations

from odda.scanner import Rule, Severity

RULES = [
    Rule(
        id="cookie.xdebug-session",
        severity=Severity.HIGH,
        title="Xdebug debug session observed (RCE candidate)",
        header_patterns=(r"(?i:^set-cookie:\s*xdebug_session=)",),
        request_query_patterns=(r"(?i:^xdebug_session_start$)",),
        request_browser_only=True,
        refs=("https://xdebug.org/docs/step_debug",),
    ),
]
