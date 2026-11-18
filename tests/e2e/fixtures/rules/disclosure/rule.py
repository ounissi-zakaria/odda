# See ADR-0035.
from __future__ import annotations

from odda.scanner import Rule, Severity

_STACK_TRACE_PATTERNS: tuple[str, ...] = (
    r"Traceback \(most recent call last\):",
    r"^\s*at [\w$.]+\(\w+\.java:\d+\)",
    r"^Fatal error: Uncaught",
    r"^#\d+ /\S+\(\d+\):",
    r"\(node:internal/[\w/]+:\d+:\d+\)",
    r"^\s*at (?:System|Microsoft)\.[\w.]+\(",
)

_PRIVATE_KEY_PATTERNS: tuple[str, ...] = (
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----",
)

_SUCCESS_STATUSES: tuple[int, ...] = tuple(range(200, 300))
_FRONTEND_CONTENT_TYPES: tuple[str, ...] = (
    "text/html",
    "text/javascript",
    "application/javascript",
    "application/x-javascript",
)
_FRONTEND_SECRET_PATTERNS: tuple[str, ...] = (
    r"""["']?client_?[Ss]ecret["']?\s*[:=]\s*["'][^"'\n]{8,}["']""",
    r"""["']?api_?[Kk]ey["']?\s*[:=]\s*["'][^"'\n]{8,}["']""",
    r"""["']?refresh_?[Tt]oken["']?\s*[:=]\s*["'][^"'\n]{8,}["']""",
    r"""(?:console\.log|logger?\.\w+)\([^)\n]*password[^\n)]*""",
)

_AWS_KEY_ID_PATTERN = r"\bAKIA[0-9A-Z]{16}\b"

RULES = [
    Rule(
        id="disclosure.stack-trace",
        severity=Severity.HIGH,
        title="Stack trace disclosed in a response body",
        body_patterns=_STACK_TRACE_PATTERNS,
        refs=("CWE-209", "https://cwe.mitre.org/data/definitions/209.html"),
    ),
    Rule(
        id="disclosure.private-key",
        severity=Severity.HIGH,
        title="Private key disclosed in a response body",
        body_patterns=_PRIVATE_KEY_PATTERNS,
        refs=("CWE-321", "https://cwe.mitre.org/data/definitions/321.html"),
    ),
    Rule(
        id="disclosure.frontend-secret",
        severity=Severity.MEDIUM,
        title="Hardcoded secret in a frontend script or page",
        body_patterns=_FRONTEND_SECRET_PATTERNS,
        statuses=_SUCCESS_STATUSES,
        content_types=_FRONTEND_CONTENT_TYPES,
        refs=("CWE-798", "https://cwe.mitre.org/data/definitions/798.html"),
    ),
    Rule(
        id="disclosure.aws-key",
        severity=Severity.HIGH,
        title="AWS access key id disclosed in a response",
        body_patterns=(_AWS_KEY_ID_PATTERN,),
        header_patterns=(_AWS_KEY_ID_PATTERN,),
        refs=("CWE-798", "https://cwe.mitre.org/data/definitions/798.html"),
    ),
]
