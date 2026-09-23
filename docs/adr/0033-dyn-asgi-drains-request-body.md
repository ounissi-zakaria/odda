# The dyn server's ASGI app drains the request body before responding

The dyn server (ADR-0008) is a ~15-line ASGI app that answers from the
query string alone. It never called `receive()`. An ASGI app may return
without consuming the request body, but under hypercorn that handed the
stream's fate to a race: hypercorn completes the response, drops the
stream, and *then* dispatches the client's still-in-flight DATA frames
(`DataReceived` branch of `hypercorn/protocol/h2.py`, which indexes
`self.streams[event.stream_id]` without a membership check) — `KeyError`,
which propagates out of the connection's `TaskGroup` and tears the whole
connection down mid-response.

That race is exactly what odda's H2 repeat tests provoke:
`request_send(repeat=N)` fires N requests on one connection, splitting
each body into "everything but the last byte" plus a final last-byte
write (ADR-0020's single-packet technique). When the server won the race
the client saw `connection closed before response complete` — odda
reporting truthfully — and `test_20_request_repeat.py` failed. Measured
on this box with a fixed six-process CPU load: **3/10 runs failed against
the unfixed fixture, 0/10 against the fixed one**. It was the root cause
of the "load-sensitive test" flake, not ambient load: the same module and
the same signature accounted for the `-n 4` full-suite failure that had
pushed the worker pin down to 3.

The app now consumes the request body before sending its response —
`receive()` until `more_body` is false, returning early on
`http.disconnect` — so the stream stays alive until the client's
`end_stream`. The drain sits *after* the `?race=` marker write, so the
arrival timestamps those tests assert are untouched; only the response
moves later, which is when any real server answers anyway.

## Considered Options

- Leave the app body-agnostic and keep treating the failure as a known
  load flake — rejected: the mechanism was a fixture bug reproducible in
  3/10 loaded runs, and it silently poisoned worker-count measurements
  (the reason 4 workers looked unsafe).
- Keep the fixture but send each request body in a single write — rejected:
  the two-write last-byte trick is odda's documented race technique
  (ADR-0020); losing it would delete the behaviour under test.
- Make odda's client tolerate or retry a dropped concurrent stream —
  rejected: the server really did close the connection mid-response.
  Reporting it is correct, and a retry would mask the race those tests
  exist to measure.
