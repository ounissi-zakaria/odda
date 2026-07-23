# odda's proxy skips inbound HTTP header validation

odda's mitmproxy wrapper sets `validate_inbound_headers=False`, so the
proxy does not enforce RFC 7540 §8.1.2.5 (no leading/trailing whitespace
in HTTP/2 header values) or the broader HTTP/1.1 header-syntax checks
that mitmproxy applies to inbound messages. This is the right default
for odda's audience — bug-bounty hunters and testers pointing at real,
broken, legacy, and non-conformant servers — who would otherwise hit a
502 "HTTP/2 protocol error: Received header value surrounded by
whitespace" the moment a server emits a header like `X-UA: IE=Edge` (with
a leading space) over HTTP/2. The `h2` library mitmproxy uses to parse
HTTP/2 frames rejects such values with `ProtocolError`, and mitmproxy
surfaces that as a flow error and a 502 to the client. HTTP/1.1 parsing
is lenient here — it calls `value.strip()` and silently drops the
whitespace — so the failure is HTTP/2-specific. mitmproxy's
`validate_inbound_headers` option (registered by the proxyserver addon
during `DumpMaster` construction) gates both the `h2` library's own
validation and mitmproxy's HTTP/1.1 `validate_headers()` call; setting
it to `False` makes the proxy accept the non-conformant value and
forward the response. It must be set after `DumpMaster` construction,
since the option is unknown before the addon registers it. The trade-off
is correctness/security: disabling this check makes mitmproxy "vulnerable
to HTTP smuggling attacks" per the option's own help text. We accept
that because that risk is a server-side concern and odda is a local,
interactive client tool run by the operator against hosts they control,
not a production proxy — the same class of trade-off already accepted
for `ssl_insecure` (see ADR-0007). HTTP/2 upstream negotiation and
capture remain functional, and odda's raw `request send` HTTP/2 path is
unaffected since it uses its own `h2` sender, not the proxy.