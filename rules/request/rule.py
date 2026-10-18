"""Request-family rules (ADR-0035's signal bar), as an installable slot.

All three read the request side of a flow, and the request side is
trusted material only when a browser produced it: an agent's own
crafted ``request_send`` probes carry the same shapes by construction
(a token in the URL while testing for token-in-URL), so the whole
family carries the browser-attribution gate and probes are
structurally invisible. What fires is directly actionable —
credentials interceptable in transit (cleartext Authorization),
session tokens and passwords landing in logs, history, and Referer
headers — which is the bar. The parameter-name patterns are anchored
full-name matches on the spellings real frameworks emit, not
substrings prose would trip (``assetsid`` is not a session id).
"""

from __future__ import annotations

from odda.scanner import Rule, Severity

# Session-token-shaped query parameter names: the framework session
# ids (Django/JSP/PHP/ASP.NET — ``sess\w*`` keeps the session* family)
# plus the token/auth spellings whose presence in a URL is itself the
# leak (reset links, bearer tokens pasted into browsers).
_SESSION_TOKEN_PARAM: tuple[str, ...] = (
    r"(?i:^(?:sess\w*|phpsessid|jsessionid|asp\.net_sessionid"
    r"|token|access_token|auth_token|authtoken|id_token|auth)$)",
)

# Password-shaped query parameter names.
_PASSWORD_PARAM: tuple[str, ...] = (r"(?i:^(?:pass|pwd|passwd|password\w*)$)",)

RULES = [
    Rule(
        id="request.cleartext-authorization",
        severity=Severity.HIGH,
        title="Authorization header sent over cleartext HTTP",
        request_header_patterns=(r"(?i:^authorization:)",),
        schemes=("http",),
        browser_only=True,
        refs=("CWE-319", "https://cwe.mitre.org/data/definitions/319.html"),
    ),
    Rule(
        id="request.session-token-in-url",
        severity=Severity.MEDIUM,
        title="Session token passed in the URL query string",
        request_query_patterns=_SESSION_TOKEN_PARAM,
        browser_only=True,
        refs=("CWE-598", "https://cwe.mitre.org/data/definitions/598.html"),
    ),
    Rule(
        id="request.password-in-url",
        severity=Severity.MEDIUM,
        title="Password passed in the URL query string",
        request_query_patterns=_PASSWORD_PARAM,
        browser_only=True,
        refs=("CWE-598", "https://cwe.mitre.org/data/definitions/598.html"),
    ),
]
