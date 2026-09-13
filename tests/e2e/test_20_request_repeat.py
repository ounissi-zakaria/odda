"""e2e port of scrut 20-request-repeat.md: concurrent repeat sends.

``request_send(name, repeat=N)`` fires N concurrent copies of one
request — the race / limit-overrun path (ADR-0020). H2 uses stream
multiplexing with the last-byte single-packet technique (all N HEADERS
in one TLS record, observable via the dyn server's ``?race=`` arrival
timestamps); H1 opens N parallel connections. Multi-name + repeat and
pipelining + repeat are rejected pre-emptively; fix_content_length +
repeat is allowed.
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e.conftest import dyn_server, odda_session


async def _new(h, name: str, port: int) -> None:
    await h.call(
        "request_new", {"name": name, "host": "127.0.0.1", "port": port, "force": True}
    )


def _race_spread_ms(race_file: Path) -> float:
    """Max-min arrival spread in ms from the dyn server's timestamp file."""
    ts = [int(line) for line in race_file.read_text().splitlines()]
    return (max(ts) - min(ts)) / 1_000_000


async def test_repeat_h2_five_concurrent_copies(odda_session, tmp_path) -> None:
    """H2 repeat 5: 5 records, all 200, distinct ids, tight arrival window."""
    race_dir = tmp_path / "race"
    async with odda_session() as h, dyn_server(race_dir=race_dir) as dyn:
        await _new(h, "h2-race", dyn.port)
        h.write_request(
            "h2-race",
            (
                b"GET /a?body=ok&status=200"
                b"&header=Content-Type:application/json&race=h2 HTTP/2\r\n"
                b"user-agent: odda-test\r\naccept: */*\r\n\r\n"
            ),
        )
        recs = await h.call_json(
            "request_send",
            {"name": "h2-race", "repeat": 5, "insecure": True, "timeout": 10},
        )
        assert isinstance(recs, list) and len(recs) == 5
        assert all(r["status_code"] == 200 for r in recs)
        assert len({r["id"] for r in recs}) == 5

        # single-packet property: clear the file first, send, spread < 50ms
        (race_dir / "h2").unlink(missing_ok=True)
        await h.call_json(
            "request_send",
            {"name": "h2-race", "repeat": 5, "insecure": True, "timeout": 10},
        )
        assert _race_spread_ms(race_dir / "h2") < 50


async def test_repeat_h1_parallel_connections(odda_session, tmp_path) -> None:
    """H1 repeat 5: N parallel connections; validated by 5 records all 200."""
    async with odda_session() as h, dyn_server() as dyn:
        await _new(h, "h1-race", dyn.port)
        h.write_request(
            "h1-race",
            (
                f"GET /a?body=ok&status=200"
                f"&header=Content-Type:application/json&race=h1 HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{dyn.port}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode(),
        )
        recs = await h.call_json(
            "request_send",
            {"name": "h1-race", "repeat": 5, "insecure": True, "timeout": 10},
        )
        assert len(recs) == 5
        assert all(r["status_code"] == 200 for r in recs)


async def test_repeat_h2_post_body_single_packet(odda_session, tmp_path) -> None:
    """Last-byte single-packet holds with a POST body: 4 records, tight spread."""
    race_dir = tmp_path / "race"
    async with odda_session() as h, dyn_server(race_dir=race_dir) as dyn:
        await _new(h, "h2-post", dyn.port)
        h.write_request(
            "h2-post",
            (
                f"POST /a?body=posted&status=200"
                f"&header=Content-Type:application/json&race=h2post HTTP/2\r\n"
                f"Host: 127.0.0.1:{dyn.port}\r\n"
                f"Content-Type: application/x-www-form-urlencoded\r\n"
                f"Content-Length: 10\r\n\r\ncoupon=X20"
            ).encode(),
        )
        (race_dir / "h2post").unlink(missing_ok=True)
        recs = await h.call_json(
            "request_send",
            {"name": "h2-post", "repeat": 4, "insecure": True, "timeout": 10},
        )
        assert len(recs) == 4
        assert all(r["status_code"] == 200 for r in recs)
        assert _race_spread_ms(race_dir / "h2post") < 50


async def test_repeat_h2_large_body(odda_session, tmp_path) -> None:
    """A 4KB body (multi DATA frames) with repeat 3 still completes."""
    async with odda_session() as h, dyn_server() as dyn:
        await _new(h, "h2-big", dyn.port)
        body = b"x" * 4096
        h.write_request(
            "h2-big",
            (
                f"POST /a?body=big-ok&status=200"
                f"&header=Content-Type:text/plain&race=h2big HTTP/2\r\n"
                f"Host: 127.0.0.1:{dyn.port}\r\n"
                f"Content-Type: application/octet-stream\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode()
            + body,
        )
        recs = await h.call_json(
            "request_send",
            {"name": "h2-big", "repeat": 3, "insecure": True, "timeout": 10},
        )
        assert len(recs) == 3
        assert all(r["status_code"] == 200 for r in recs)


async def test_repeat_fix_content_length_allowed(odda_session, tmp_path) -> None:
    """fix_content_length + repeat: CL recomputed once, N copies fired."""
    async with odda_session() as h, dyn_server() as dyn:
        await _new(h, "cl-repeat", dyn.port)
        h.write_request(
            "cl-repeat",
            (
                f"POST /a?body=cl-rep-ok&status=200"
                f"&header=Content-Type:application/json HTTP/2\r\n"
                f"Host: 127.0.0.1:{dyn.port}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: 999\r\n\r\n"
                f'{{"k":"v"}}'
            ).encode(),
        )
        recs = await h.call_json(
            "request_send",
            {
                "name": "cl-repeat",
                "repeat": 3,
                "fix_content_length": True,
                "insecure": True,
                "timeout": 10,
            },
        )
        assert len(recs) == 3
        assert all(r["status_code"] == 200 for r in recs)


async def test_repeat_combo_rejections(odda_session, tmp_path) -> None:
    """repeat + names and repeat + pipelining are rejected pre-emptively."""
    async with odda_session() as h, dyn_server() as dyn:
        for n in ("h2-race", "h1-race"):
            await _new(h, n, dyn.port)
        h.write_request(
            "h2-race",
            b"GET /a?body=ok&status=200&race=h2 HTTP/2\r\nuser-agent: odda-test\r\n\r\n",
        )
        h.write_request(
            "h1-race",
            (
                f"GET /a?body=ok&status=200&race=h1 HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{dyn.port}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode(),
        )
        err = await h.call_error(
            "request_send",
            {"name": "h2-race", "names": ["h1-race"], "repeat": 3, "insecure": True},
        )
        assert "single-name only" in err

        err = await h.call_error(
            "request_send",
            {"name": "h2-race", "repeat": 3, "pipelining": True, "insecure": True},
        )
        assert "cannot be combined" in err


async def test_repeat_one_is_single_shot(odda_session, tmp_path) -> None:
    """repeat=1 (default) keeps the frozen single-name contract: one dict."""
    async with odda_session() as h, dyn_server() as dyn:
        await _new(h, "h2-race", dyn.port)
        h.write_request(
            "h2-race",
            b"GET /a?body=ok&status=200&race=h2 HTTP/2\r\nuser-agent: odda-test\r\n\r\n",
        )
        r = await h.call_json(
            "request_send",
            {"name": "h2-race", "repeat": 1, "insecure": True, "timeout": 10},
        )
        assert isinstance(r, dict)
