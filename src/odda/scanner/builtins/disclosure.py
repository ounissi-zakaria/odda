"""Disclosure-family builtin rules (ADR-0035's signal bar).

Each pattern below must be actionable when it fires — specific, rare,
exploitable or leakable. Frame shapes carry the specificity (a
``.java:`` line reference, a ``#N /path:`` PHP frame, a
``node:internal/`` module path), not bare words like "error" that
prose and minified JS would trip.
"""

from __future__ import annotations

from odda.scanner.model import Rule, Severity

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

RULES: tuple[Rule, ...] = (
    Rule(
        id="disclosure.stack-trace",
        severity=Severity.HIGH,
        title="Stack trace disclosed in a response body",
        body_patterns=_STACK_TRACE_PATTERNS,
        refs=("CWE-209", "https://cwe.mitre.org/data/definitions/209.html"),
    ),
)
