"""Supply-chain-family builtin rules (ADR-0035's signal bar).

The 2024 polyfill.io supply-chain incident: one operator was caught
serving mobile-malware redirects from polyfill.io and its Chinese CDN
mirrors (BootCDN, Bootcss, Staticfile — .org and .net — and Uncdn).
A page importing scripts from that set is compromised supply, not a
style choice; the fingerprint is the script ``src`` itself.
"""

from __future__ import annotations

import re

from odda.scanner.model import Rule, Severity

# Domains the incident reporting caught serving the payload: Sansec's
# polyfill.io research plus the BootCDN/Bootcss/Staticfile mirrors the
# follow-up reporting traced to the same operator.
_MALICIOUS_CDN_DOMAINS = (
    "polyfill\\.io",
    "bootcdn\\.net",
    "bootcss\\.com",
    "staticfile\\.org",
    "staticfile\\.net",
    "uncdn\\.com",
)
_DOMAIN_ALTERNATION = "|".join(_MALICIOUS_CDN_DOMAINS)

# A <script src="..."> whose URL hosts on one of the compromised
# domains. The domain must sit at a URL boundary — preceded by the
# scheme/host separator, a subdomain dot, or the attribute quote, and
# not running into another word character — so `notpolyfill.io` or
# `evil-polyfill.io.example` cannot trip it, while the incident's
# canonical `cdn.bootcdn.net`-style hosts do. A page merely mentioning
# a domain has no script-tag shape and never fires.
_SCRIPT_SRC_FROM_MALICIOUS_CDN = re.compile(
    rf"""<script\b[^>]{{0,300}}\ssrc\s*=\s*["'][^"']*"""
    rf"""(?<![\w-])(?:{_DOMAIN_ALTERNATION})(?![\w.-])[^"']*["']""",
    re.IGNORECASE,
)

RULES: tuple[Rule, ...] = (
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
)
