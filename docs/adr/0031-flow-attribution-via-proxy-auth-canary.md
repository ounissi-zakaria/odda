# Flow attribution rides proxy credentials, seeded by a canary request

Every browser launches with Playwright proxy credentials whose username
is its browser identifier token. A mitmproxy addon sits ahead of
FlowFileAddon: it parses `Proxy-Authorization: Basic` on every request,
stamps the parsed identity onto `flow.metadata["proxyauth"]`, pops the
header, and otherwise forwards unconditionally — no authentication is
ever required of any client. FlowRecordWriter reads that metadata and
writes `browser_id` into the flows.jsonl record (null when absent:
request_send, hand-routed curl, pre-seed traffic).

Chrome only transmits proxy credentials after receiving a 407, and
each browser gets a fresh temp profile with a cold auth cache — so
`browser_open` seeds it deterministically: right after launch, odda
drives the initial page to `http://odda-seed.invalid/canary`. The
addon answers the unauthenticated canary with a synthesized 407,
Playwright's auth handler supplies the credentials, Chrome retries,
the addon stamps and acknowledges with a synthesized 200. The host is
RFC 2606-reserved and both responses are synthesized locally, so the
canary never touches DNS or the network. From then on Chrome sends the
credentials preemptively on every connection (profile-wide auth
cache). Both canary legs are tagged `odda_auth_challenge` in flow
metadata and skipped by FlowFileAddon at allocation, so they never
produce flow dirs or jsonl lines. Seeding failure is silent: flows
degrade to null attribution rather than wrong attribution.

The mechanism was chosen over three measured alternatives.
Per-browser proxy listeners attribute at accept time with total
coverage but require mode-list surgery on every upstream flip, a
port→browser registry, and listener lifecycle for zero attribution
gain. Context-level extra HTTP headers (CDP
`Network.setExtraHTTPHeaders` replayed by the driver) have no
attach-race and are immune to page-JS forgery, but service-worker
script downloads and browser-stack telemetry go untagged, and the
marker physically transits the wire — a strip miss leaks a harness
fingerprint to a target. Passive 407 challenge windows attribute well
but challenge every loopback client, breaking bare curl through
`proxy_url` whenever a browser is cold. Proxy credentials carry the
identity out-of-band: `Proxy-Authorization` is a Fetch-forbidden
header (page JS cannot forge or duplicate it), mitmproxy consumes it
so it reaches neither the target nor the capture file, and no
challenge mode ever exists for curl to hit.

The trade-offs: launch-time telemetry dialed before seeding completes
lands null-attributed rather than browser-tagged (it is the noise the
flows doc already teaches agents to grep past); seeding has no
success signal in the `browser_open` result, so a broken auth handler
shows up only as null attribution (visible, not wrong); a local
process could self-attribute by sending the token as its proxy
username (same trust domain as odda itself — out of threat model);
and attribution correctness is coupled to the pinned driver's proxy
auth machinery, joining the existing patchright drift-audit surface.

The decision is recorded because the canary goto in `browser_open`
looks inexplicable without this context, the accept-all addon looks
like a security regression rather than an attribution carrier, and a
future contributor might delete the seeding trip as dead code — each
silently degrading flows.jsonl attribution to nulls.
