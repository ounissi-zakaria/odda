from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from mcp import Client

from odda.scanner.userrules import load_slot_source

if TYPE_CHECKING:
    from collections.abc import Iterator

    from odda.scanner.model import Rule

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "e2e" / "fixtures"


def pick_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_tcp(port: int, attempts: int = 400, delay: float = 0.03) -> None:
    last: OSError | None = None
    for _ in range(attempts):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError as exc:
            last = exc
            time.sleep(delay)
    raise RuntimeError(f"server on port {port} not reachable: {last}")


def _launch_server_threaded(
    cmd: list[str], env: dict[str, str] | None = None
) -> ServerHandle:
    port = pick_port()
    argv = [a.replace("{port}", str(port)) for a in cmd]
    proc = subprocess.Popen(  # noqa: S603
        argv,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    _wait_tcp(port)
    return ServerHandle(proc, port)


async def curl(
    url: str,
    proxy: str,
    *,
    insecure: bool = True,
    extra: list[str] | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess:
    cmd = ["curl", "-s", "--max-time", str(timeout), "-x", proxy]
    if insecure:
        cmd += ["-k", "--proxy-insecure"]
    return await asyncio.to_thread(
        subprocess.run,  # noqa: S603
        [*cmd, *(extra or []), url],
        capture_output=True,
        text=True,
        check=False,
    )


class ServerHandle:
    def __init__(self, proc: subprocess.Popen, port: int, path: str = "") -> None:
        self.proc = proc
        self.port = port
        self.path = path

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.path}"

    @property
    def tls_base(self) -> str:
        return f"https://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


async def _stop(handle: ServerHandle) -> None:
    await asyncio.to_thread(handle.stop)


@asynccontextmanager
async def fixture_site(
    filenames: Iterable[str] = (), index_body: str | None = None
) -> AsyncIterator[ServerHandle]:
    key = (tuple(sorted(filenames)), index_body)
    root_dir, root = await asyncio.to_thread(_boot_site_root)
    path = await asyncio.to_thread(_site_dir, root_dir, key)
    yield ServerHandle(root.proc, root.port, path)


def _boot_site_root() -> tuple[Path, ServerHandle]:
    global _SITE_SERVER, _SITE_ROOT  # noqa: PLW0603 — process-wide scratch root
    with _SHARED_LOCK:
        handle = _SITE_SERVER
        if (
            handle is not None
            and _SITE_ROOT is not None
            and handle.proc is not None
            and handle.proc.poll() is None
        ):
            return _SITE_ROOT, handle
        if _SITE_ROOT is None:
            _SITE_ROOT = Path(tempfile.mkdtemp(prefix="odda-sites-"))
        _SITE_SERVER = _launch_server_threaded(
            [
                sys.executable,
                "-m",
                "http.server",
                "{port}",
                "--bind",
                "127.0.0.1",
                "--directory",
                str(_SITE_ROOT),
            ]
        )
        return _SITE_ROOT, _SITE_SERVER


def _site_dir(root: Path, key: tuple[tuple[str, ...], str | None]) -> str:
    filenames, index_body = key
    with _SHARED_LOCK:
        name = _SITE_DIRS.get(key)
        if name is None:
            name = f"site{len(_SITE_DIRS) + 1}"
            site = root / name
            site.mkdir(parents=True)
            for f in filenames:
                (site / f).symlink_to(FIXTURES / f)
            if index_body is not None:
                (site / "index.html").write_text(index_body)
            _SITE_DIRS[key] = name
        return f"/{name}"


def _boot_dyn_server(race_dir: Path | None) -> ServerHandle:
    key, cert = _tls_cert()
    env = {**os.environ}
    if race_dir is not None:
        env["ODDA_RACE_DIR"] = str(race_dir)
    return _launch_server_threaded(
        [
            sys.executable,
            "-m",
            "hypercorn",
            "--bind",
            "127.0.0.1:{port}",
            "--keyfile",
            str(key),
            "--certfile",
            str(cert),
            f"{FIXTURES / 'dyn_asgi.py'}:app",
        ],
        env=env,
    )


def _shared_dyn_server() -> ServerHandle:
    handle = _DYN_SHARED.get("default")
    if handle is None or handle.proc.poll() is not None:
        handle = _boot_dyn_server(None)
        _DYN_SHARED["default"] = handle
    return handle


def _stop_shared_servers() -> None:
    stop_handles = [*_DYN_SHARED.values(), *([_SITE_SERVER] if _SITE_SERVER else [])]
    for handle in stop_handles:
        with suppress(Exception):
            handle.stop()
    _DYN_SHARED.clear()
    if _SITE_ROOT is not None:
        with suppress(Exception):
            shutil.rmtree(_SITE_ROOT, ignore_errors=True)


_SITE_SERVER: ServerHandle | None = None
_SITE_DIRS: dict[tuple[tuple[str, ...], str | None], str] = {}
_DYN_SHARED: dict[str, ServerHandle] = {}
_SHARED_LOCK = threading.Lock()
_SITE_ROOT: Path | None = None
atexit.register(_stop_shared_servers)


@asynccontextmanager
async def dyn_server(race_dir: Path | None = None) -> AsyncIterator[ServerHandle]:
    if race_dir is None:
        yield _shared_dyn_server()
        return
    handle = await asyncio.to_thread(_boot_dyn_server, race_dir)
    try:
        yield handle
    finally:
        await _stop(handle)


@asynccontextmanager
async def script_server(script: str, *args: str) -> AsyncIterator[ServerHandle]:
    handle = await asyncio.to_thread(
        _launch_server_threaded,
        [sys.executable, str(FIXTURES / script), "{port}", *args],
    )
    try:
        yield handle
    finally:
        await _stop(handle)


class Harness:
    # See ADR-0027.

    def __init__(self, client: Client, data_dir: Path, config_home: Path) -> None:
        self.client = client
        self.data_dir = data_dir
        self.config_home = config_home

    async def call(self, name: str, args: dict | None = None) -> object:
        # See ADR-0027.
        r = await self.client.call_tool(name, args or {})
        assert not r.is_error, f"{name} errored: {[c.text for c in r.content]}"
        sc = r.structured_content
        if sc is None and r.content:
            # See ADR-0027.
            return r.content[0].text
        if isinstance(sc, dict) and list(sc) == ["result"]:
            return sc["result"]
        return sc

    async def call_error(self, name: str, args: dict | None = None) -> str:
        r = await self.client.call_tool(name, args or {})
        assert r.is_error, f"{name} unexpectedly succeeded: {r.structured_content}"
        text = r.content[0].text if r.content else ""
        prefix = f"Error executing tool {name}: "
        return text.removeprefix(prefix)

    async def open_browser(self, url: str | None = None) -> tuple[str, int]:
        r = await self.call("browser_open", {})
        bid, tid = r["browser_id"], r["tab_id"]
        assert re.fullmatch(r"[a-z]{5}", bid), f"browser_id not a token: {bid!r}"
        if url is not None:
            await self.call("navigate", {"browser_id": bid, "tab_id": tid, "url": url})
        return bid, tid

    async def eval(self, bid: str, tid: int, js: str) -> str:
        r = await self.client.call_tool(
            "eval", {"browser_id": bid, "tab_id": tid, "js": js}
        )
        assert not r.is_error, f"eval errored: {[c.text for c in r.content]}"
        return r.content[0].text

    async def wait_for(
        self, bid: str, tid: int, expression: str, timeout: float = 10
    ) -> str:
        r = await self.client.call_tool(
            "wait_for",
            {
                "browser_id": bid,
                "tab_id": tid,
                "expression": expression,
                "timeout": timeout,
            },
        )
        assert not r.is_error, f"wait_for errored: {[c.text for c in r.content]}"
        return r.content[0].text

    async def navigate(self, bid: str, tid: int, url: str, **kwargs: object) -> dict:
        return await self.call(
            "navigate", {"browser_id": bid, "tab_id": tid, "url": url, **kwargs}
        )

    async def page_snapshot(self, bid: str, tid: int) -> str:
        return await self.call("page_snapshot", {"browser_id": bid, "tab_id": tid})

    def write_request(self, name: str, raw: bytes) -> Path:
        d = self.data_dir / "requests" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "request").write_bytes(raw)
        return d / "request"

    def _flows_lines(self) -> list[str]:
        f = self.data_dir / "flows" / "flows.jsonl"
        if not f.is_file():
            return []
        return f.read_text().splitlines()

    def flow_record(self, flow_id: str) -> dict:
        for line in self._flows_lines():
            rec = json.loads(line)
            if rec["id"] == flow_id:
                return rec
        raise AssertionError(f"flow {flow_id} not in flows.jsonl")

    def flow_count(self) -> int:
        return len(self._flows_lines())

    def flows_with(self, marker: str) -> list[dict]:
        return [json.loads(l) for l in self._flows_lines() if marker in l]

    async def wait_flow(
        self, marker: str, attempts: int = 200, delay: float = 0.05
    ) -> dict:
        for _ in range(attempts):
            flows = self.flows_with(marker)
            if flows:
                return flows[0]
            await asyncio.sleep(delay)
        raise AssertionError(f"no flow matching {marker!r} flushed to disk")

    def _findings_lines(self) -> list[str]:
        f = self.data_dir / "scanner" / "findings.jsonl"
        if not f.is_file():
            return []
        return f.read_text().splitlines()

    def findings_for(self, flow_id: str) -> list[dict]:
        out: list[dict] = []
        for l in self._findings_lines():
            rec = json.loads(l)
            if rec.get("flow_id") == flow_id:
                out.append(rec)
        return out

    async def wait_finding(
        self, flow_id: str, attempts: int = 200, delay: float = 0.05
    ) -> dict:
        for _ in range(attempts):
            findings = self.findings_for(flow_id)
            if findings:
                return findings[0]
            await asyncio.sleep(delay)
        raise AssertionError(f"no finding for flow {flow_id!r} in findings.jsonl")


def self_signed_cert(tmp_dir: Path) -> tuple[Path, Path]:
    key, cert = tmp_dir / "srv.key", tmp_dir / "srv.pem"
    subprocess.run(  # noqa: S603
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return key, cert


def _tls_cert() -> tuple[Path, Path]:
    global _CERT_PAIR  # noqa: PLW0603 — memo slot, written once under the lock
    with _CERT_LOCK:
        if _CERT_PAIR is None:
            cert_dir = Path(tempfile.mkdtemp(prefix="odda-tls-"))
            _CERT_PAIR = self_signed_cert(cert_dir)
            atexit.register(shutil.rmtree, cert_dir, ignore_errors=True)
        return _CERT_PAIR


_CERT_LOCK = threading.Lock()
_CERT_PAIR: tuple[Path, Path] | None = None


@asynccontextmanager
async def slow_load_server(site_dir: Path) -> AsyncIterator[ServerHandle]:
    handle = await asyncio.to_thread(
        _launch_server_threaded,
        [
            sys.executable,
            str(FIXTURES / "slow_load_server.py"),
            "{port}",
            str(site_dir),
        ],
    )
    try:
        yield handle
    finally:
        await _stop(handle)


@pytest.fixture(autouse=True)
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config-home"
    home.mkdir()
    monkeypatch.setenv("ODDA_CONFIG_HOME", str(home))
    return home


@pytest.fixture
def odda_session(tmp_path: Path, config_home: Path) -> Iterator[object]:
    import odda.mcp as odda_mcp
    from odda import flowstore

    data_dir = tmp_path / ".odda"

    @asynccontextmanager
    async def _session() -> AsyncIterator[Harness]:
        old_cwd = Path.cwd()
        os.chdir(tmp_path)
        try:
            async with Client(odda_mcp.mcp_server) as client:
                assert flowstore.DATA_DIR == data_dir, (
                    "lifespan did not set the data dir for this session"
                )
                yield Harness(client, data_dir, config_home)
        finally:
            os.chdir(old_cwd)

    return _session


@pytest.fixture
def odda_subprocess_session(tmp_path: Path, config_home: Path) -> Iterator[object]:
    from mcp.client.stdio import StdioServerParameters

    src = Path(__file__).resolve().parents[2] / "src"
    assert src.is_dir(), f"checkout src not found at {src}"

    @asynccontextmanager
    async def _session(cwd: Path | None = None) -> AsyncIterator[Harness]:
        cwd = tmp_path if cwd is None else cwd
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "odda", "mcp"],
            env={
                "PYTHONPATH": str(src),
                "ODDA_CONFIG_HOME": str(config_home),
            },
            cwd=cwd,
        )
        async with Client(params) as client:
            yield Harness(client, cwd / ".odda", config_home)

    return _session


# See ADR-0036.
_RULES_SRC = Path(__file__).parent / "fixtures" / "rules"


def _slot_rules(family: str) -> list[Rule]:
    path = _RULES_SRC / family / "rule.py"
    return load_slot_source(family, path.read_text(encoding="utf-8"), str(path))


async def _seed_slot(h: Harness, family: str) -> dict:
    return await h.call(
        "rule_install",
        {"name": family, "file": str(_RULES_SRC / family / "rule.py")},
    )


def _findings_index(data_dir: Path) -> list[dict]:
    f = data_dir / "scanner" / "findings.jsonl"
    if not f.is_file():
        return []
    return [json.loads(line) for line in f.read_text().splitlines()]


def sans_display_urls(rows):
    return [
        {k: v for k, v in row.items() if k not in ("vnc_url", "novnc_url")}
        for row in rows
    ]


def _stored_flow(
    data_dir: Path,
    flow_id: str,
    body: bytes = b"payload",
    *,
    content_type: str = "text/plain",
    status_code: int = 200,
    query: str | None = None,
) -> dict:
    flow_dir = data_dir / "flows" / flow_id
    flow_dir.mkdir(parents=True)
    (flow_dir / "response_body.txt").write_bytes(body)
    header_block = (
        f"HTTP/1.1 {status_code} X\r\n"
        f"Content-Type: {content_type}\r\n"
        + ("X-T14-Hit: yes\r\n" if query is not None else "")
        + "\r\n"
    )
    (flow_dir / "response_headers").write_bytes(header_block.encode())
    path = f"/{flow_id}.txt" + (f"?{query}" if query else "")
    if query is not None:
        (flow_dir / "request").write_bytes(
            f"GET {path} HTTP/1.1\r\nHost: fixture.local\r\n\r\n".encode()
        )
    return {
        "id": flow_id,
        "method": "GET",
        "scheme": "http",
        "host": "fixture.local",
        "port": 80,
        "path": path,
        "browser_id": None,
        "status_code": status_code,
        "total_duration_ms": 1.0,
        "body_file": f"flows/{flow_id}/response_body.txt",
        "error": None,
    }


__all__ = [
    "FIXTURES",
    "Harness",
    "ServerHandle",
    "config_home",
    "curl",
    "dyn_server",
    "fixture_site",
    "odda_session",
    "odda_subprocess_session",
    "script_server",
    "self_signed_cert",
    "slow_load_server",
]
