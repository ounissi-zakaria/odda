"""Fingerprint-family rules (ADR-0035's signal bar), as an installable slot.

Each rule here keys on a product fingerprint that confirms a
known-exploitable surface — a teardown cookie only Apache Shiro sends,
a version-carrying Telerik handler URL, a metrics exposition only a
Prometheus endpoint emits. The fingerprint carries the specificity, so
the patterns never fire on prose that merely mentions the product.
"""

from __future__ import annotations

from odda.scanner import Rule, Severity

# Telerik UI for ASP.NET AJAX embeds its assembly identity in the
# WebResource.axd script URL (URL-encoded: `...Telerik.Web.UI%2c+
# Version%3d2013.2.611.35%2c+Culture%3dneutral`). Every release before
# R2 2017 SP1 (2017.2.621) is vulnerable to CVE-2017-9248 — years
# 2007-2016, the R1 2017 line, and the 2017.2.503 build.
_TELERIK_VULN_VERSIONS = (
    r"(?:(?:200[7-9]|201[0-6])\.\d{1,3}\.\d{2,4}|2017\.1\.\d{3}|2017\.2\.503)"
    r"(?:\.\d{2,4})?"
)

# A Prometheus exposition documents every metric family with adjacent
# `# HELP <name> <doc>` / `# TYPE <name> <type>` lines. Requiring the
# pair under one well-known client_golang prefix (go_, process_,
# prometheus_) makes prose about "metrics" structurally unable to
# match: there is no such line pair, and no typed name to bind.
_PROMETHEUS_EXPOSITION = (
    r"(?m)^# HELP ((?:prometheus|go|process)_[a-zA-Z0-9_:]+)[^\r\n]*\r?\n"
    r"# TYPE \1 (?:counter|gauge|histogram|summary)\b"
)

RULES = [
    Rule(
        id="deser.apache-shiro-rememberme",
        severity=Severity.MEDIUM,
        title="Apache Shiro rememberMe teardown cookie (deserialization surface)",
        # Shiro clears a failed rememberMe cookie by responding with
        # `Set-Cookie: rememberMe=deleteMe` — the fingerprint of a
        # rememberMe deserialization candidate (the cookie body is
        # AES-encrypted serialized Java on the vulnerable line).
        header_patterns=(r"(?i)^set-cookie:[^\r\n]*\brememberMe=deleteMe\b",),
        refs=(
            "CVE-2016-4437",
            "CWE-502",
            "https://cwe.mitre.org/data/definitions/502.html",
        ),
    ),
    Rule(
        id="cve.telerik-ui-9248",
        severity=Severity.HIGH,
        title="Telerik UI for ASP.NET AJAX vulnerable version (CVE-2017-9248)",
        # One pattern so the handler reference and a pre-fix version
        # must co-occur: a fixed or unknown version never satisfies the
        # version alternation, so it cannot fire on Telerik presence
        # alone. The scan from the axd anchor is lazy, skipping any
        # intervening non-Telerik `Version=` tokens (System.Web
        # extensions report 4.0.0.0 shapes).
        body_patterns=(
            rf"""(?is)Telerik\.Web\.UI\.WebResource\.axd[^"'>]{{0,600}}?"""
            rf"""Version(?:%3[dD]|=){_TELERIK_VULN_VERSIONS}""",
        ),
        refs=(
            "CVE-2017-9248",
            "CWE-502",
            "https://nvd.nist.gov/vuln/detail/CVE-2017-9248",
        ),
    ),
    Rule(
        id="exposure.prometheus-metrics",
        severity=Severity.MEDIUM,
        title="Prometheus metrics endpoint exposed",
        body_patterns=(_PROMETHEUS_EXPOSITION,),
        statuses=(200,),
        refs=(
            "CWE-200",
            "https://cwe.mitre.org/data/definitions/200.html",
        ),
    ),
]
