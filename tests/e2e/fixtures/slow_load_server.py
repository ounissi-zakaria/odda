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
    def do_GET(self) -> None:
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
        pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    port = int(sys.argv[1])
    site_dir = sys.argv[2]
    os.chdir(site_dir)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), SlowLoadHandler)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
