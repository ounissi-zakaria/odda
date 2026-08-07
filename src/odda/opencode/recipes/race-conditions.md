# Recipe: Race conditions / limit overrun (concurrent send)

A worked example of `odda request send --repeat N` to race a server-side
check-then-write window (the classic limit-overrun attack — a "once per
user" coupon applied N times because N concurrent requests all pass the
check before any write lands). The same pattern applies to rate-limit
bypass and any TOCTOU window where the same request fired many times at
once slips through. See [SKILL.md](../SKILL.md) for the command surface
and [REQUEST.md](../REQUEST.md) for the `--repeat` reference.

The workflow: capture the target request, clone it as an editable
request, fire N concurrent copies over HTTP/2 (single-packet
stream-multiplex), count the successes.

## 1. Capture the target request from the browser

Navigate and trigger the action once (e.g. apply a coupon) so the request
is captured as a flow. Find the flow in `.odda/flows/flows.jsonl`.

```
odda navigate --url https://target/ --browser-id 1 --tab-id 1
# ... trigger the action in the page (click the apply-coupon button) ...
odda eval --js "document.querySelector('button.apply')" --browser-id 1 --tab-id 1
```

Find the coupon-apply flow (the POST to the coupon endpoint with the
CSRF token and coupon code).

## 2. Clone the flow as an editable request

Clone the captured flow so you can re-send its exact bytes (including
the auth cookie and CSRF token, which are in the request body/headers).

```
# Find the flow id (the most recent POST to /cart/coupon)
odda --json status > /dev/null   # just to confirm the server is up
# Read flows.jsonl to find the coupon POST:
rg '"method":"POST".*"path":"/cart/coupon"' .odda/flows/flows.jsonl | tail -1
```

```
odda request clone --flow-id <flow-id> --name coupon --force
```

The cloned request carries the session cookie and CSRF token in its
bytes — `request send` is out-of-page (raw bytes, no browser), so the
auth is in the request file, not pulled from the browser at send time.

## 3. Fire N concurrent copies over HTTP/2

`--repeat N` opens one HTTP/2 connection and sends N copies as
concurrent streams with the last-byte single-packet technique (all N
HEADERS frames in one TLS record) so they arrive at the server
near-simultaneously. If the request line says `HTTP/1.1`, edit it to
`HTTP/2` first (H2 single-packet is what makes the race reliable; H1
`--repeat` opens N parallel connections with residual TLS-handshake
spread).

```
# If the cloned request line says HTTP/1.1, switch to HTTP/2 for the race:
sed -i 's/HTTP\/1.1/HTTP\/2/' .odda/requests/coupon/request

# Fire 20 concurrent copies:
odda --json request send --name coupon --repeat 20 --insecure --timeout 15 \
  > /tmp/race-result.json
```

## 4. Count the successes

Each of the 20 copies gets its own flow record (one-request-one-response).
Read back how many succeeded (e.g. a 302 "coupon applied" vs a 200 "already
applied" or 409 "limit reached"). Filter `flows.jsonl` for the race's
host/path and count the status codes.

```
python3 -c '
import json
results = json.load(open("/tmp/race-result.json"))
applied = sum(1 for r in results if r["status_code"] == 302)
denied = sum(1 for r in results if r["status_code"] in (200, 409))
print(f"applied={applied} denied={denied}")
'
```

If the race slipped through the window, `applied` will be > 1 (the
single-packet technique aims for all N to land in the window; in practice
3-15 typically succeed against a naive check-then-write, which is enough
to overrun a "once per user" limit). If only 1 succeeded, re-run — the
race is probabilistic against real servers.

## When to use `--repeat` vs `odda eval Promise.all(fetch)`

`--repeat` is out-of-page: raw bytes, no browser cookies — the
Burp-Repeater model. Use it when you've cloned the request (the auth is
in the bytes) or when the endpoint doesn't need cookies.

Use `odda eval` running `Promise.all(Array(N).fill().map(()=>fetch(url,{...})))`
from the page when the request is cookie-gated and you want the browser
to carry the session automatically. The browser's HTTP/2 multiplexing
also delivers near-simultaneously, though without the single-packet
guarantee (the browser flushes each fetch's frames separately, so
there's more spread than `--repeat`'s single-TLS-record flush).

**Footgun:** concurrent `odda eval` calls on the same tab **serialize** —
"race harder by firing parallel evals" silently does nothing. Use one
`eval` with `Promise.all` for concurrency, not many parallel `eval`
calls.

## Multi-endpoint races (N different requests concurrently)

For state-machine races where N *different* endpoints must be hit
concurrently (e.g. login + checkout racing the same state transition),
use multi-name over HTTP/2: author each request as its own editable
request file, then `--name a --name b --name c ...` with `HTTP/2` request
lines sends them as concurrent streams on one connection (the same
single-packet technique).

```
odda request clone --flow-id <login-flow-id> --name login --force
odda request clone --flow-id <checkout-flow-id> --name checkout --force
# Ensure both request lines say HTTP/2
odda --json request send --name login --name checkout --insecure --timeout 15
```