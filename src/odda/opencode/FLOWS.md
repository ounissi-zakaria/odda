# Proxy and flow capture

`odda proxy-url` — Return the HTTP proxy URL as plain text. Route HTTP clients through this URL to capture traffic.

Captured flows are stored as read-only files under `.odda/flows/`. This is the reference for the flow file layout and `flows.jsonl` schema; see [SKILL.md](SKILL.md) for the quick-reference table and Targeting model.

**Tip:** headless Chrome emits background telemetry (safebrowsing, update checks, account pings) on `browser open`, producing 100+ flows before you navigate anywhere. Grep `flows.jsonl` for your target host to filter: `grep <host> .odda/flows/flows.jsonl`.

## Flow file layout

```
.odda/flows/
├── flows.jsonl                # append-only index, one JSON line per completed/errored flow
└── <NNNNN>/                   # zero-padded monotonic flow id (e.g. 00001)
    ├── request                # reconstructed HTTP request (request line + headers + blank line + decoded body), CRLF
    ├── meta.json              # read-only sidecar with {"scheme":"https","host":"...","port":443}
    ├── response_headers       # reconstructed status line + headers + blank line, CRLF (no body)
    ├── response_body.<ext>    # decoded response body, ext from Content-Type (e.g. .json, .html, .bin); omitted for excluded/empty bodies
    └── error                  # present only on errored flows (e.g. server unreachable)
```

## `flows.jsonl` schema

One JSON object per line, in completion order:

```json
{
  "id": "00042",
  "method": "GET",
  "scheme": "https",
  "host": "example.com",
  "port": 443,
  "path": "/",
  "status_code": 200,
  "total_duration_ms": 12.3,
  "body_file": "flows/00042/response_body.json",
  "error": null
}
```

- `id` — zero-padded flow id matching the directory name; lets you re-sort by capture order with `sort`.
- `scheme` / `port` — request scheme (`http`/`https`) and port. Populated for new captures and `odda request send` flows; absent on records written by older odda versions (treat as `https`/`443`).
- `status_code` — `null` for errored flows (the `error` field holds the message instead).
- `body_file` — path relative to `.odda`; read it as `read ".odda/$body_file"`. `null` when the body was excluded (images/video/audio/fonts) or empty. **Note:** the exclusion only applies to browser-captured flows. `odda request send` always stores the response body regardless of `Content-Type` — a hand-built request exists to see its body (e.g. a path-traversal file mislabeled `image/jpeg`), so `body_file` is non-null for any non-empty `request send` response.
- `error` — `null` for completed flows; the error message for failed flows.

## Response bodies (decoding note)

`response_body.<ext>` holds the **decoded** body (gzip/br/deflate inflated). The `response_headers` file shows the original on-wire headers, so `Content-Encoding: gzip` and the compressed `Content-Length` may not match the decoded body file. This is expected.

Per-flow files (`request`, `response_headers`, `response_body.*`, `error`) are written read-only so history cannot be edited.