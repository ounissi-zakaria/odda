# See ADR-0035.
from __future__ import annotations

from odda.scanner import Rule, Severity

RULES = [
    Rule(
        id="takeover.s3-nosuchbucket",
        severity=Severity.HIGH,
        title="S3 NoSuchBucket error exposes a registerable dangling bucket",
        body_patterns=(
            r"(?s)<Code>\s*NoSuchBucket\s*</Code>.{0,500}?<BucketName>[^<]{1,255}",
        ),
        refs=("https://can-i-take-over-xyz.vercel.app/",),
    ),
    Rule(
        id="takeover.shopify",
        severity=Severity.HIGH,
        title="Shopify shop-not-found page exposes a dangling shop domain",
        body_patterns=(r"Sorry, this shop is currently unavailable",),
        refs=("https://can-i-take-over-xyz.vercel.app/",),
    ),
]
