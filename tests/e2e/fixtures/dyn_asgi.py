from __future__ import annotations

import gzip
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

WS_SERVER_HELLO_TEXT = "s2c-hello"
WS_SERVER_HELLO_BYTES = b"\x83\xa4kind\x00\x01\xfe\xffs2c-binary\xff"


async def ws_echo(scope: dict[str, Any], receive: Any, send: Any) -> None:
    while True:
        message = await receive()
        kind = message["type"]
        if kind == "websocket.connect":
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.send", "text": WS_SERVER_HELLO_TEXT})
            await send({"type": "websocket.send", "bytes": WS_SERVER_HELLO_BYTES})
        elif kind == "websocket.receive":
            if message.get("text") is not None:
                await send({"type": "websocket.send", "text": message["text"]})
            else:
                await send({"type": "websocket.send", "bytes": message["bytes"]})
        elif kind == "websocket.disconnect":
            return


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] == "websocket":
        await ws_echo(scope, receive, send)
        return
    await http_dyn(scope, receive, send)


async def http_dyn(scope: dict[str, Any], receive: Any, send: Any) -> None:
    raw_qs = scope.get("query_string", b"")
    qd = urllib.parse.parse_qs(
        raw_qs.decode() if raw_qs else "", keep_blank_values=True
    )
    race_id = qd.get("race", [None])[0]
    if race_id:
        race_dir = Path(os.environ.get("ODDA_RACE_DIR", "/tmp/odda-race"))
        race_dir.mkdir(parents=True, exist_ok=True)
        with (race_dir / race_id).open("a") as f:
            f.write(f"{time.monotonic_ns()}\n")
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        if not message.get("more_body", False):
            break
    body = qd.get("body", [""])[0].encode()
    status = int(qd.get("status", ["200"])[0])
    headers: list[tuple[bytes, bytes]] = []
    for h in qd.get("header", []):
        if ":" in h:
            name, value = h.split(":", 1)
            headers.append((name.strip().lower().encode(), value.strip().encode()))
    if not any(name == b"content-type" for name, _ in headers):
        headers.append((b"content-type", b"text/plain; charset=utf-8"))
    if "gzip" in qd:
        body = gzip.compress(body)
        headers.append((b"content-encoding", b"gzip"))
    headers.append((b"content-length", str(len(body)).encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
