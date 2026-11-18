# See ADR-0020.
from __future__ import annotations

from pathlib import Path

from tests.e2e.conftest import dyn_server, odda_session


async def _new(h, name: str, port: int) -> None:
    await h.call(
        "request_new", {"name": name, "host": "127.0.0.1", "port": port, "force": True}
    )


def _race_spread_ms(race_file: Path) -> float:
    ts = [int(line) for line in race_file.read_text().splitlines()]
    return (max(ts) - min(ts)) / 1_000_000


async def test_h2_multiname_concurrent_streams(odda_session, tmp_path) -> None:
    race_dir = tmp_path / "race"
    async with odda_session() as h, dyn_server(race_dir=race_dir) as dyn:
        for n, path, body in (
            ("h2-a", "/a", "aaa"),
            ("h2-b", "/b", "bbb"),
            ("h2-c", "/c", "ccc"),
        ):
            await _new(h, n, dyn.port)
            h.write_request(
                n,
                (
                    f"GET {path}?body={body}&status=200"
                    f"&header=Content-Type:application/json&race=h2mn HTTP/2\r\n"
                    f"user-agent: odda-test\r\n\r\n"
                ).encode(),
            )
        recs = await h.call(
            "request_send",
            {"names": ["h2-a", "h2-b", "h2-c"], "insecure": True, "timeout": 10},
        )
        assert isinstance(recs, list) and len(recs) == 3
        assert all(r["status_code"] == 200 for r in recs)
        assert len({r["id"] for r in recs}) == 3
        first_body = (
            h.data_dir / "flows" / recs[0]["id"] / "response_body.json"
        ).read_bytes()
        assert first_body == b"aaa"

        (race_dir / "h2mn").unlink(missing_ok=True)
        await h.call(
            "request_send",
            {"names": ["h2-a", "h2-b", "h2-c"], "insecure": True, "timeout": 10},
        )
        assert _race_spread_ms(race_dir / "h2mn") < 50


async def test_h1_multiname_stays_sequential(odda_session, tmp_path) -> None:
    async with odda_session() as h, dyn_server() as dyn:
        for n, body in (("h1-a", "h1a"), ("h1-b", "h1b")):
            await _new(h, n, dyn.port)
            h.write_request(
                n,
                (
                    f"GET /a?body={body}&status=200"
                    f"&header=Content-Type:application/json HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{dyn.port}\r\n"
                    f"Connection: keep-alive\r\n\r\n"
                ).encode(),
            )
        recs = await h.call(
            "request_send",
            {"names": ["h1-a", "h1-b"], "insecure": True, "timeout": 10},
        )
        assert isinstance(recs, list) and len(recs) == 2
        assert all(r["status_code"] == 200 for r in recs)


async def test_h2_multiname_rejections(odda_session, tmp_path) -> None:
    async with odda_session() as h, dyn_server() as dyn:
        for n in ("h2-a", "h2-b", "h1-a"):
            await _new(h, n, dyn.port)
        h.write_request(
            "h2-a",
            (
                b"GET /a?body=aaa&status=200"
                b"&header=Content-Type:application/json&race=h2mn HTTP/2\r\n"
                b"user-agent: odda-test\r\n\r\n"
            ),
        )
        h.write_request(
            "h2-b",
            (
                b"GET /b?body=bbb&status=200"
                b"&header=Content-Type:application/json&race=h2mn HTTP/2\r\n"
                b"user-agent: odda-test\r\n\r\n"
            ),
        )
        h.write_request(
            "h1-a",
            (
                f"GET /a?body=h1a&status=200"
                f"&header=Content-Type:application/json HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{dyn.port}\r\n"
                f"Connection: keep-alive\r\n\r\n"
            ).encode(),
        )

        err = await h.call_error(
            "request_send",
            {"names": ["h1-a", "h2-a"], "insecure": True, "timeout": 10},
        )
        assert "mix" in err

        await h.call_error(
            "request_send",
            {
                "names": ["h2-a", "h2-b"],
                "pipelining": True,
                "insecure": True,
                "timeout": 10,
            },
        )
