# odda's proxy skips upstream TLS certificate validation

odda's mitmproxy wrapper sets `ssl_insecure=True`, so the proxy does not
verify upstream TLS certificates when forwarding requests. This is the
right default for odda's audience — bug-bounty hunters testing against
staging boxes, internal hosts, and appliances with self-signed or
expired certs — who would otherwise be unable to proxy to the very hosts
they are testing. The trade-off is correctness/security: a man-in-the-
middle attacker between odda and the upstream would not be detected by
mitmproxy. We accept that because odda is a local, interactive tool run
by the operator against hosts they control, not a production proxy;
end-to-end upstream validation is the caller's responsibility, not the
proxy's.