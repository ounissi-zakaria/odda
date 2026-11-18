# See ADR-0035.
from __future__ import annotations

import re

from odda.scanner import Rule, Severity

_MALICIOUS_CDN_DOMAINS = (
    "polyfill\\.io",
    "bootcdn\\.net",
    "bootcss\\.com",
    "staticfile\\.org",
    "staticfile\\.net",
    "uncdn\\.com",
)
_DOMAIN_ALTERNATION = "|".join(_MALICIOUS_CDN_DOMAINS)

_SCRIPT_SRC_FROM_MALICIOUS_CDN = re.compile(
    rf"""<script\b[^>]{{0,300}}\ssrc\s*=\s*["'][^"']*"""
    rf"""(?<![\w-])(?:{_DOMAIN_ALTERNATION})(?![\w.-])[^"']*["']""",
    re.IGNORECASE,
)

RULES = [
    Rule(
        id="js.malicious-cdn-import",
        severity=Severity.HIGH,
        title="Script imported from a compromised CDN (2024 supply-chain set)",
        body_patterns=(_SCRIPT_SRC_FROM_MALICIOUS_CDN,),
        refs=(
            "CWE-494",
            "https://sansec.io/research/polyfill-supply-chain-attack",
        ),
    ),
]
