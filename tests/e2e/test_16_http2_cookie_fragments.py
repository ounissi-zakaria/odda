from __future__ import annotations

import asyncio
import subprocess
import sys

from tests.e2e.conftest import FIXTURES, curl, dyn_server


def _count_header_lines(raw: bytes, name: str) -> int:
    lname = name.lower().encode()
    return sum(
        1 for line in raw.split(b"\r\n") if line.lower().startswith(lname + b":")
    )


async def test_duplicate_cookie_headers_stay_split(odda_session, tmp_path) -> None:
    async with odda_session() as h, dyn_server() as dyn:
        proxy = await h.call("proxy_url", {})

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
    async with odda_session() as h, dyn_server() as dyn:
        proxy = await h.call("proxy_url", {})

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
