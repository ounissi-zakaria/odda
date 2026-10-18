"""Takeover-family rules (ADR-0035's signal bar), as an installable slot.

The bodies below confirm a registerable dangling resource: the
service's own "this resource is gone" answer, naming the resource.
Takeover checkers key on exactly these strings; a finding means
registering the name would claim the traffic that resolves to it.
"""

from __future__ import annotations

from odda.scanner import Rule, Severity

RULES = [
    Rule(
        id="takeover.s3-nosuchbucket",
        severity=Severity.HIGH,
        title="S3 NoSuchBucket error exposes a registerable dangling bucket",
        # The S3 error envelope for a missing bucket: the Code first,
        # then the BucketName whose DNS the caller resolves. One
        # pattern so both markers must co-occur — Code alone can be
        # prose about S3, BucketName alone appears in unrelated S3
        # errors — and the match spans to the bucket name so evidence
        # carries the registerable value.
        body_patterns=(
            r"(?s)<Code>\s*NoSuchBucket\s*</Code>.{0,500}?<BucketName>[^<]{1,255}",
        ),
        refs=("https://can-i-take-over-xyz.vercel.app/",),
    ),
    Rule(
        id="takeover.shopify",
        severity=Severity.HIGH,
        title="Shopify shop-not-found page exposes a dangling shop domain",
        # Shopify's exact shop-unavailable wording; a custom-404 page
        # cannot reuse it because the string is served by Shopify's
        # edge, not the shop's theme.
        body_patterns=(r"Sorry, this shop is currently unavailable",),
        refs=("https://can-i-take-over-xyz.vercel.app/",),
    ),
]
