"""Shared e2e harness: real MCP server in-process, real Chrome, real fixture servers.

Maps the scrut suite's per-document model onto pytest:

- **Per-test sessions.** Each test enters ``async with odda_session() as h:``
  inside its own body — the session (and its lifespan: real Chrome +
  real mitmproxy) is created and torn down within one task, the pattern
  the SDK's own interaction tests use (anyio cancel scopes must exit in
  the task that entered them, so pytest fixtures can't outlive the
  test). One session = one data dir = one browser set: every test is
  self-contained, no inter-test state coupling.
- **Data-dir isolation via cwd.** ``odda.mcp.resolve_data_dir`` is
  cwd-``.odda``-relative and the lifespan calls
  ``flowstore.set_data_dir`` at session start, so the session factory
  chdirs into a per-test tmp dir for the session's lifetime. xdist
  workers run tests sequentially, so the process-global cwd is never
  contended.
- **Real subprocess fixture servers.** ``fixture_site`` (plain HTTP
  serving the repo's fixture files), ``dyn_server`` (hypercorn HTTPS,
  H1+H2 via ALPN, dynamic ``?body=&status=&header=&gzip=1&race=``
  responses), and ``script_server`` (raw-socket fixtures). Their
  blocking launch parts run in worker threads — the event loop that
  carries the in-process mitmproxy must never block. Booting is
  amortized where it is pure overhead: the request-stateless
  ``dyn_server`` keeps one instance per worker process (``_shared_dyn_server``)
  and the TLS cert pair is generated once per process (``_tls_cert``).
"""

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

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "e2e" / "fixtures"


def pick_port() -> int:
    """Bind a socket to get an OS-assigned free port (same trick as scrut's pick_port)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_tcp(port: int, attempts: int = 400, delay: float = 0.03) -> None:
    """Bounded poll until a TCP listener answers; never sleeps past readiness."""
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
    """Launch a fixture server in a worker thread; return once its port answers."""
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
    """curl through the proxy; the blocking subprocess call runs in a thread."""
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
    """A running fixture-server subprocess with an auto-picked port."""

    def __init__(self, proc: subprocess.Popen, port: int, path: str = "") -> None:
        self.proc = proc
        self.port = port
        self.path = path

    @property
    def base(self) -> str:
        """Plain-http base URL for this server."""
        return f"http://127.0.0.1:{self.port}{self.path}"

    @property
    def tls_base(self) -> str:
        """https base URL (for servers speaking TLS)."""
        return f"https://127.0.0.1:{self.port}"

    def stop(self) -> None:
        """Terminate the server; bounded wait; hard-kill if needed."""
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


async def _stop(handle: ServerHandle) -> None:
    """Stop a server from async code without blocking the loop."""
    await asyncio.to_thread(handle.stop)


@asynccontextmanager
async def fixture_site(
    filenames: Iterable[str] = (), index_body: str | None = None
) -> AsyncIterator[ServerHandle]:
    """Serve FIXTURES/<name> files over plain HTTP (http.server).

    One server per worker process serves a scratch root that holds one
    directory per distinct site content — the directory is named after the
    content key (see :func:`_site_dir`) and contains exactly the requested
    files, so a test still sees only its own file set. Booting an
    interpreter + ``http.server`` per content (``["index.html"]`` alone
    appears in 37 tests) bought nothing over a path segment under one
    server.
    """
    key = (tuple(sorted(filenames)), index_body)
    root_dir, root = await asyncio.to_thread(_boot_site_root)
    path = await asyncio.to_thread(_site_dir, root_dir, key)
    yield ServerHandle(root.proc, root.port, path)


def _boot_site_root() -> tuple[Path, ServerHandle]:
    """Boot (once per worker) the http.server that serves every site dir."""
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
    """Materialize one site content key as a directory under the site root.

    Returns the URL path it is served under. Files are symlinked from
    FIXTURES (no copy) and an ``index_body`` is written out; the directory
    holds nothing else, so the served file set stays exactly the key's.
    """
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
    """Boot a hypercorn HTTPS server (H1+H2 via ALPN) on the shared cert."""
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
    """Return the worker's long-lived dyn server, booting it on first use.

    Booting costs an interpreter, hypercorn, and a TLS key exchange
    (~0.4 s per test that serves dynamic responses), and dyn_asgi is
    request-stateless — every behavior is a query parameter — so one
    instance per worker process serves every caller that needs no private
    race dir. A dead instance (crashed server) is rebooted in place.
    """
    handle = _DYN_SHARED.get("default")
    if handle is None or handle.proc.poll() is not None:
        handle = _boot_dyn_server(None)
        _DYN_SHARED["default"] = handle
    return handle


def _stop_shared_servers() -> None:
    """Reap the shared fixture servers at process exit (they are daemons)."""
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
    """Hypercorn HTTPS server (H1+H2 via ALPN) serving dyn_asgi with a self-signed cert.

    ``race_dir`` callers get a private instance (the race behavior lives in
    the server's environment) that is stopped with the block; everyone else
    shares the worker's instance (see :func:`_shared_dyn_server`).
    """
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
    """Run a FIXTURES server script (close_without_response, echo_path_h2server, …)."""
    handle = await asyncio.to_thread(
        _launch_server_threaded,
        [sys.executable, str(FIXTURES / script), "{port}", *args],
    )
    try:
        yield handle
    finally:
        await _stop(handle)


class Harness:
    """One MCP session + one data dir; the assertion conventions every test shares.

    ``call`` asserts the tool *succeeded* and returns the natural value:
    dict results pass through (structuredContent); unstructured tools
    (eval, wait_for as ``-> Any``; the 13 text-first tools via
    ``structured_output=False`` — ADR 0023) come back as their text,
    which ``call_json`` parses. ``call_error``
    asserts the call *failed* and returns the error text — the #03
    contract: odda messages verbatim after the SDK's ``Error executing
    tool <name>: `` prefix, param names never ``--flag`` spellings.
    """

    def __init__(self, client: Client, data_dir: Path) -> None:
        self.client = client
        self.data_dir = data_dir

    async def call(self, name: str, args: dict | None = None) -> object:
        """Call a tool, assert success, return the natural value (dict via
        structuredContent; text-first tools as their text — use call_json)."""
        r = await self.client.call_tool(name, args or {})
        assert not r.is_error, f"{name} errored: {[c.text for c in r.content]}"
        sc = r.structured_content
        if sc is None and r.content:
            # Unstructured tools (eval/wait_for as -> Any; the 13
            # structured_output=False tools — ADR 0023) are text-only.
            return r.content[0].text
        return sc

    async def call_json(self, name: str, args: dict | None = None) -> object:
        """Call a text-first (unstructured) tool and parse its JSON text."""
        text = await self.call(name, args)
        assert isinstance(text, str), f"{name}: expected JSON text, got {type(text)}"
        return json.loads(text)

    async def call_error(self, name: str, args: dict | None = None) -> str:
        """Call a tool, assert failure, return the error text (SDK prefix stripped)."""
        r = await self.client.call_tool(name, args or {})
        assert r.is_error, f"{name} unexpectedly succeeded: {r.structured_content}"
        text = r.content[0].text if r.content else ""
        prefix = f"Error executing tool {name}: "
        return text.removeprefix(prefix)

    async def open_browser(self, url: str | None = None) -> tuple[str, int]:
        """Open a browser; optionally navigate its initial tab; return (browser_id, tab_id)."""
        r = await self.call("browser_open", {"headless": True})
        bid, tid = r["browser_id"], r["tab_id"]
        assert re.fullmatch(r"[a-z]{5}", bid), f"browser_id not a token: {bid!r}"
        if url is not None:
            await self.call("navigate", {"browser_id": bid, "tab_id": tid, "url": url})
        return bid, tid

    async def eval(self, bid: str, tid: int, js: str) -> str:
        """eval is Any-annotated → text-only; assert not-error and return the text."""
        r = await self.client.call_tool(
            "eval", {"browser_id": bid, "tab_id": tid, "js": js}
        )
        assert not r.is_error, f"eval errored: {[c.text for c in r.content]}"
        return r.content[0].text

    async def wait_for(
        self, bid: str, tid: int, expression: str, timeout: float = 10
    ) -> str:
        """wait_for is Any-annotated → text-only."""
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
        """navigate (default wait_until=load); extra kwargs pass through."""
        return await self.call(
            "navigate", {"browser_id": bid, "tab_id": tid, "url": url, **kwargs}
        )

    async def page_snapshot(self, bid: str, tid: int) -> str:
        """page_snapshot is text-first (ADR 0023) → the tree arrives as text."""
        return await self.call("page_snapshot", {"browser_id": bid, "tab_id": tid})

    def write_request(self, name: str, raw: bytes) -> Path:
        """Write agent-authored wire bytes into requests/<name>/request."""
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
        """Return the flows.jsonl record for flow_id."""
        for line in self._flows_lines():
            rec = json.loads(line)
            if rec["id"] == flow_id:
                return rec
        raise AssertionError(f"flow {flow_id} not in flows.jsonl")

    def flow_count(self) -> int:
        """Number of records in flows.jsonl (0 when absent)."""
        return len(self._flows_lines())

    def flows_with(self, marker: str) -> list[dict]:
        """All flows.jsonl records whose line contains marker."""
        return [json.loads(l) for l in self._flows_lines() if marker in l]

    async def wait_flow(
        self, marker: str, attempts: int = 200, delay: float = 0.05
    ) -> dict:
        """Bounded poll until a flow matching marker lands in flows.jsonl (mitmproxy flush)."""
        for _ in range(attempts):
            flows = self.flows_with(marker)
            if flows:
                return flows[0]
            await asyncio.sleep(delay)
        raise AssertionError(f"no flow matching {marker!r} flushed to disk")


def self_signed_cert(tmp_dir: Path) -> tuple[Path, Path]:
    """Generate a self-signed cert/key pair (CN=127.0.0.1) into tmp_dir; returns paths."""
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
    """Return a process-wide self-signed cert/key pair, generating it once.

    RSA-2048 keygen is ~0.15 s of CPU, and the fixture TLS servers only need
    *a* valid 127.0.0.1 cert, so one pair per worker process serves every
    boot (removed at process exit by the atexit sweep below).
    """
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
    """Serve the SPA slow-load fixture (site_dir + /slow-image?sleep=N)."""
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


@pytest.fixture
def odda_session(tmp_path: Path) -> Iterator[object]:
    """Per-test MCP session factory: ``async with odda_session() as h:``.

    A sync fixture returning an async context manager: the session is
    entered and exited inside the test's own task (the SDK's pattern —
    anyio cancel scopes cannot cross tasks), with cwd moved into the
    test's tmp dir for the session's lifetime so ``resolve_data_dir``
    isolates this test's ``.odda``.
    """
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
                yield Harness(client, data_dir)
        finally:
            os.chdir(old_cwd)

    return _session


__all__ = [
    "FIXTURES",
    "Harness",
    "ServerHandle",
    "curl",
    "dyn_server",
    "fixture_site",
    "odda_session",
    "script_server",
    "self_signed_cert",
    "slow_load_server",
]
