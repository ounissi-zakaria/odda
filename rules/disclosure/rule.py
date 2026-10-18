"""Disclosure-family rules (ADR-0035's signal bar), as an installable slot.

Each pattern below must be actionable when it fires — specific, rare,
exploitable or leakable. Frame shapes carry the specificity (a
``.java:`` line reference, a ``#N /path:`` PHP frame, a
``node:internal/`` module path), not bare words like "error" that
prose and minified JS would trip. Credential rules lean on armor and
assignment shapes for the same reason: PEM ``BEGIN`` sentinels, the
``AKIA`` key alphabet, quoted secret assignments — shapes real leaks
have and ordinary prose does not.
"""

from __future__ import annotations

from odda.scanner import Rule, Severity

_STACK_TRACE_PATTERNS: tuple[str, ...] = (
    # Python interpreter traceback banner.
    r"Traceback \(most recent call last\):",
    # Java frame: "at com.example.Foo.run(Bar.java:42)".
    r"^\s*at [\w$.]+\(\w+\.java:\d+\)",
    # PHP fatal with an uncaught exception (PHP 5+ wording).
    r"^Fatal error: Uncaught",
    # PHP frame: "#0 /var/www/html/index.php(23): handler()".
    r"^#\d+ /\S+\(\d+\):",
    # Node frame internals: "(node:internal/modules/cjs/loader:1105:14)".
    r"\(node:internal/[\w/]+:\d+:\d+\)",
    # .NET frame: "at System.Runtime.CompilerServices.TaskAwaiter.GetResult()".
    r"^\s*at (?:System|Microsoft)\.[\w.]+\(",
)

_PRIVATE_KEY_PATTERNS: tuple[str, ...] = (
    # PEM armor for any private-key flavor: PKCS#1 (RSA/DSA), SEC1
    # (EC), PKCS#8 ("PRIVATE KEY", "ENCRYPTED PRIVATE KEY"), and
    # OpenSSH. The word list admits one label per flavor; PUBLIC KEY
    # and CERTIFICATE armor never carry "PRIVATE KEY".
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----",
)

# 2xx statuses only — error pages quoting config are a different signal.
_SUCCESS_STATUSES: tuple[int, ...] = tuple(range(200, 300))
_FRONTEND_CONTENT_TYPES: tuple[str, ...] = (
    "text/html",
    "text/javascript",
    "application/javascript",
    "application/x-javascript",
)
_FRONTEND_SECRET_PATTERNS: tuple[str, ...] = (
    # Secret assignment with a quoted value of secret-ish length; the
    # assignment operator directly after the identifier is what keeps
    # ordinary key-flavored names (apiKeyId, keyword, keydown) out.
    r"""["']?client_?[Ss]ecret["']?\s*[:=]\s*["'][^"'\n]{8,}["']""",
    r"""["']?api_?[Kk]ey["']?\s*[:=]\s*["'][^"'\n]{8,}["']""",
    r"""["']?refresh_?[Tt]oken["']?\s*[:=]\s*["'][^"'\n]{8,}["']""",
    # A logger call leaking a password value.
    r"""(?:console\.log|logger?\.\w+)\([^)\n]*password[^\n)]*""",
)

# AWS access key id: "AKIA" + 16 uppercase alphanumerics, delimited so
# truncated, lowercase, and embedded look-alikes stay out.
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
