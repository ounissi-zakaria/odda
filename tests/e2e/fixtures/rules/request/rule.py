# See ADR-0035.
from __future__ import annotations

from odda.scanner import Rule, Severity

_SESSION_TOKEN_PARAM: tuple[str, ...] = (
    r"(?i:^(?:sess\w*|phpsessid|jsessionid|asp\.net_sessionid"
    r"|token|access_token|auth_token|authtoken|id_token|auth)$)",
)

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
