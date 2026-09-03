"""Captured flows emit duplicate headers per-line, not comma-folded (scrut 16).

Flow serialization writes each duplicate header on its own line,
faithful to what the client/server sent: two `cookie` fragments (a raw
h2 client splitting pairs per RFC 7540 §8.1.2.5) appear as two
`cookie:` lines in the captured `request`; two `Set-Cookie` headers (a
routine H1 login-response shape) appear as two `set-cookie:` lines in
the captured `response_headers`. Never the pre-fix `, `-fold.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

from tests.e2e.conftest import FIXTURES, curl, dyn_server


def _count_header_lines(raw: bytes, name: str) -> int:
    """Count `name:` header lines in raw captured bytes, case-insensitive."""
    lname = name.lower().encode()
    return sum(
        1 for line in raw.split(b"\r\n") if line.lower().startswith(lname + b":")
    )


async def test_duplicate_cookie_headers_stay_split(odda_session, tmp_path) -> None:
    """Two cookie fragments in the captured request → two cookie: lines, no fold."""
    async with odda_session() as h, dyn_server() as dyn:
        proxy = await h.call("proxy_url", {})

        # Request side: raw h2 client (curl/Chrome pre-join cookies) sends
        # `session=abc` and `_lab=val` as two cookie fragments.
        proc = await asyncio.to_thread(
            subprocess.run,  # noqa: S603
            [
                sys.executable,
                str(FIXTURES / "h2_cookie_client.py"),
                proxy,
                "127.0.0.1",
                str(dyn.port),
                "/?marker=h2-cookie-join",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc.returncode == 0, proc.stderr

        rec = await h.wait_flow("h2-cookie-join")
        raw = (h.data_dir / "flows" / rec["id"] / "request").read_bytes()
        assert _count_header_lines(raw, "cookie") == 2
        assert b"session=abc, _lab" not in raw


async def test_duplicate_set_cookie_headers_stay_split(odda_session, tmp_path) -> None:
    """Two Set-Cookie response headers → two set-cookie: lines, no fold."""
    async with odda_session() as h, dyn_server() as dyn:
        proxy = await h.call("proxy_url", {})

        # Response side: two ?header=set-cookie: params (URL-encoded so
        # the `; Path=/` attribute survives the query string) produce two
        # Set-Cookie response headers on the H1 dyn server.
        url = (
            f"{dyn.tls_base}/?marker=h2-setcookie-split"
            "&header=set-cookie:a%3D1%3B%20Path%3D/"
            "&header=set-cookie:b%3D2%3B%20Path%3D/"
        )
        r = await curl(
            url,
            proxy,
            extra=["--http2", "-o", "/dev/null", "-w", "curl_status=%{http_code}"],
        )
        assert r.stdout.strip().endswith("curl_status=200"), (
            f"curl through proxy failed: {r.stdout!r} {r.stderr!r}"
        )

        rec = await h.wait_flow("h2-setcookie-split")
        raw = (h.data_dir / "flows" / rec["id"] / "response_headers").read_bytes()
        assert _count_header_lines(raw, "set-cookie") == 2
        assert b"a=1; Path=/, b=2" not in raw
