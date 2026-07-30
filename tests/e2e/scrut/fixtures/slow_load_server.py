"""Slow-load fixture server for navigate timeout/wait-until tests.

Serves a single-page app whose inline JS runs immediately (sets
``window.__spaReady`` and writes to ``#app``) but whose ``<img>``
points at ``/slow-image?sleep=N``, delaying ``window.onload`` by N
seconds. This reproduces the SPA slow-load scenario where the page is
ready at ``DOMContentLoaded`` but Playwright's default
``wait_until="load"`` blocks until the slow image finishes (or the 30s
default timeout fires).

Run as: python3 slow_load_server.py <port> <site_dir>

Serves files from ``site_dir`` (plain files like ``spa-slow-load.html``)
and handles ``/slow-image?sleep=N`` specially by sleeping N seconds
then returning 1x1 PNG bytes. Use a ``BaseHTTPRequestHandler`` subclass
on a ``ThreadingHTTPServer`` so the slow image response on one thread
does not block serving other requests.

This is a plain HTTP/1.1 server (no TLS, no h2) — the slow-load tests
only need the timing behavior, not protocol coverage.
"""

from __future__ import annotations

import http.server
import os
import socketserver
import sys
import time
import urllib.parse

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c63f8ffff3f0005fe02fe2c40840000000049454e44"
    "ae426082"
)


class SlowLoadHandler(http.server.SimpleHTTPRequestHandler):
    """Serve plain files plus a ``/slow-image?sleep=N`` endpoint."""

    def do_GET(self) -> None:
        """Handle GET: slow-image endpoint with a sleep, else delegate."""
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/slow-image":
            qs = urllib.parse.parse_qs(parsed.query)
            sleep = float(qs.get("sleep", ["0"])[0])
            if sleep > 0:
                time.sleep(sleep)
            body = _PNG
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *args: object) -> None:
        """Silence default stderr request logging to keep test output clean."""


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threaded HTTP server so the slow image does not block other requests."""

    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    """Launch the slow-load server on the given port serving site_dir."""
    port = int(sys.argv[1])
    site_dir = sys.argv[2]
    os.chdir(site_dir)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), SlowLoadHandler)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
