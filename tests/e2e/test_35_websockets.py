# See ADR-0041.
from __future__ import annotations

import asyncio
import json

from tests.e2e.conftest import dyn_server, fixture_site, odda_session, script_server

WS_PAGE_BODY = """<!doctype html><html><body><script>
const ws = new WebSocket("{ws_url}");
ws.binaryType = "arraybuffer";
let seen = 0;
ws.onmessage = (e) => {{
  seen += 1;
  if (seen === 2) {{
    ws.send("c2s-ping");
    ws.send(new Uint8Array([0x00, 0x63, 0x6c, 0x61, 0xff, 0xfe]));
  }}
  if (seen === 4) {{
    ws.close(1000, "done");
  }}
}};
ws.onclose = () => {{ window.__wsDone = true; }};
</script></body></html>"""

SERVER_HELLO_TEXT = "s2c-hello"
SERVER_HELLO_BYTES = b"\x83\xa4kind\x00\x01\xfe\xffs2c-binary\xff"
CLIENT_BYTES = b"\x00cla\xff\xfe"

_WS_CLOSE_TIMEOUT_S = 10.0


async def _wait_ws_close(h, flow_id: str) -> list[dict]:
    path = h.data_dir / "flows" / flow_id / "ws.jsonl"
    deadline = asyncio.get_event_loop().time() + _WS_CLOSE_TIMEOUT_S
    while asyncio.get_event_loop().time() < deadline:
        if path.is_file():
            lines = [json.loads(l) for l in path.read_text().splitlines()]
            if any(l.get("type") == "close" for l in lines):
                return lines
        await asyncio.sleep(0.05)
    raise AssertionError(f"no close record in {path} after {_WS_CLOSE_TIMEOUT_S}s")


async def _run_leg(h, body: str, marker: str) -> None:
    async with fixture_site(index_body=body) as fx:
        bid, tid = await h.open_browser(f"{fx.base}/")
        await h.wait_for(bid, tid, "window.__wsDone === true")

        rec = await h.wait_flow(marker)
        assert rec["status_code"] == 101
        assert rec["ws"] is True
        assert rec["error"] is None

        lines = await _wait_ws_close(h, rec["id"])

        clients = [l for l in lines if l.get("from") == "client"]
        servers = [l for l in lines if l.get("from") == "server"]
        assert [(l["opcode"], l["content"]) for l in servers[:2]] == [
            ("text", SERVER_HELLO_TEXT),
            ("binary", SERVER_HELLO_BYTES.decode("latin-1")),
        ]
        assert [(l["opcode"], l["content"]) for l in servers[2:]] == [
            ("text", "c2s-ping"),
            ("binary", CLIENT_BYTES.decode("latin-1")),
        ]
        assert [(l["opcode"], l["content"]) for l in clients] == [
            ("text", "c2s-ping"),
            ("binary", CLIENT_BYTES.decode("latin-1")),
        ]
        assert all(isinstance(l["ts"], float) and l["ts"] > 0 for l in lines[:-1])
        close = lines[-1]
        assert close["type"] == "close"
        assert close["code"] == 1000
        assert close["closed_by_client"] is True

        await h.call("browser_close", {"browser_id": bid})


async def test_wss_capture(odda_session) -> None:
    async with odda_session() as h, dyn_server() as srv:
        body = WS_PAGE_BODY.format(
            ws_url=f"wss://127.0.0.1:{srv.port}/ws?marker-ws-e2e-wss=1"
        )
        await _run_leg(h, body, "marker-ws-e2e-wss")


async def test_ws_plain_capture(odda_session) -> None:
    async with odda_session() as h, script_server("ws_echo_plain.py") as srv:
        body = WS_PAGE_BODY.format(
            ws_url=f"ws://127.0.0.1:{srv.port}/ws?marker-ws-e2e-plain=1"
        )
        await _run_leg(h, body, "marker-ws-e2e-plain")
