# Recipe: replay and modify a captured request byte-for-byte

Clone a captured flow (or craft one from scratch) into an editable request, edit the raw bytes, send it, and read the response — bypassing the browser for exact wire control. This is the core raw-request workflow for smuggling, header injection, parser-differential, vhost/host-header, and any test where you need precise bytes on the wire. See [SKILL.md](../SKILL.md), [REQUEST.md](../REQUEST.md), and [FLOWS.md](../FLOWS.md) for the command reference.

## 1. Capture a request to clone (browser → proxy)

Navigate to the target through odda's proxy so the request is captured as a flow, then find its id in `flows.jsonl` (e.g. `grep target.example .odda/flows/flows.jsonl`).

```
odda browser open --headless
odda navigate --url https://target.example/ --browser-id 1 --tab-id 1
```

## 2. Clone the flow into an editable request

```
odda request clone --flow-id <flow-id> --name <name>
# writes .odda/requests/<name>/request (raw bytes) + meta.json (scheme/host/port)
```

Craft from scratch instead (when there's no flow to clone):

```
odda request new --name <name> --host <host> [--protocol https] [--port 443]
```

## 3. Edit the request bytes (CRLF warning)

Write the `request` file with the exact bytes you want on the wire. **Use `printf` or `sed 's/$/\r/'` — heredocs emit `\n` and will fail on the wire.** The file holds request line + headers + blank line + body, all CRLF-terminated.

```
printf 'POST / HTTP/1.1\r\nHost: target.example\r\nHeader: value\r\n\r\nbody' \
  > .odda/requests/<name>/request
xxd .odda/requests/<name>/request   # verify \r\n throughout
```

## 4. Send it

```
odda request send --name <name>            # exact bytes, no framing fixes
odda request send --name <name> --fix-content-length   # recompute CL after body edits
```

`send` is single-shot (no redirects), records the flow to `.odda/flows/<NNNNN>/`, and prints the `flows.jsonl` record. The sent request is durable before the network exchange.

## 5. Read the response

```
# from the send output: body_file is flows/<id>/response_body.<ext>
# read it as: read ".odda/<body_file>"
```

Inspect `response_body.*`, `response_headers`, and (if present) `error` under `.odda/flows/<id>/`.

## 6. Re-send if needed

Many attacks need the request sent more than once (smuggling poisons the back-end's stream — the second send observes the effect). Re-run `odda request send --name <name>`; each send is a fresh flow with its own id.

## Worked example: HTTP request smuggling (CL.TE)

Front-end reads `Content-Length`, back-end reads `Transfer-Encoding: chunked`; a `0\r\n\r\nG` body leaves a trailing `G` that prefixes the next request's method. Clone a captured `GET /`, rewrite it to the smuggling request (POST with both `Content-Length: 6` and `Transfer-Encoding: chunked`, body `0\r\n\r\nG`), and `send` twice. The second response body reads `"Unrecognized method GPOST"` — the smuggled `G` prefixed `POST`, confirming the back-end parsed the chunked body and left the prefix on the stream. Send once to poison, send again to observe: that two-send cadence is the recurring shape of CL.TE/TE.CL labs.

## Worked example: Host header authentication bypass

Clone a captured `GET /admin` (401, "only local users") from `flows.jsonl`, rewrite the `Host` header to `localhost` via `printf` (CRLF), and `send`. The app trusts the `Host` header for the "is this local?" check, so `Host: localhost` grants admin access even though the TCP destination (in `meta.json`) is still the real lab host. Re-edit the same file to `GET /admin/delete?username=carlos` (keep `Host: localhost` and the session cookie) and `send` again. One gotcha: separate multiple cookies with `; `, not `,` — Chrome sends cookies with `, ` separators, but some frameworks reject that in favor of `; `.