# See ADR-0035.
from __future__ import annotations

from odda.scanner import Rule, Severity

_TELERIK_VULN_VERSIONS = (
    r"(?:(?:200[7-9]|201[0-6])\.\d{1,3}\.\d{2,4}|2017\.1\.\d{3}|2017\.2\.503)"
    r"(?:\.\d{2,4})?"
)

_PROMETHEUS_EXPOSITION = (
    r"(?m)^# HELP ((?:prometheus|go|process)_[a-zA-Z0-9_:]+)[^\r\n]*\r?\n"
    r"# TYPE \1 (?:counter|gauge|histogram|summary)\b"
)

RULES = [
    Rule(
        id="deser.apache-shiro-rememberme",
        severity=Severity.MEDIUM,
        title="Apache Shiro rememberMe teardown cookie (deserialization surface)",
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
