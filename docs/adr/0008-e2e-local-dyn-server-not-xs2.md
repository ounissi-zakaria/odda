# e2e tests use a local dynamic HTTP/2 server instead of xs2.top

The `request clone` and `request send` test documents previously relied
on the remote `xs2.top` testing server for HTTPS, HTTP/2, gzip, and
dynamic `?body=&status=&header=` responses. That made the suite flaky:
xs2.top's TLS+H2 round trips intermittently exceeded the test timeouts
(curl's default on the capture-via-proxy step, odda's `--timeout 10` on
`request send`), producing `curl_status=000` and `status_code: None` in
roughly 2-5% of runs even at `-j1`. The failures were upstream latency
jitter, not a proxy or code bug.

We replaced the remote dependency with a local dyn server: a ~15-line
ASGI app (`tests/e2e/fixtures/dyn_asgi.py`) serving the same
dynamic-response contract, run under [hypercorn](https://hypercorn.readthedocs.io/)
(a dev dependency) which speaks HTTP/1.1 and HTTP/2 over TLS via ALPN.
The cert is self-signed and generated at launch. This eliminates the
external network dependency entirely so the suite runs offline and in
CI without flakiness from xs2.top. The trade-off is a dev dependency
(hypercorn + its transitive packages) installed in the test image only,
and ~15 lines of test fixture to maintain — the H2 framing lives in
hypercorn, not in odda's test code.

The proxy's `ssl_insecure=True` (ADR 0007) is what makes the local
self-signed server usable through mitmproxy for the capture-via-proxy
test steps.