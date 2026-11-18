# See ADR-0040.
from __future__ import annotations

import asyncio
import ctypes
import importlib.util
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

_NOVNC_DIR = Path(__file__).parent / "novnc"

# See ADR-0039, ADR-0040.
SCREEN_WIDTH = 1440
SCREEN_HEIGHT = 900

_X11_UNIX = Path("/tmp/.X11-unix")  # noqa: S108 - the X11 standard socket dir
_DISPLAY_RANGE = range(42, 100)
_DISPLAY_TRIES = 8
_START_TIMEOUT_S = 10.0
_STOP_TIMEOUT_S = 5.0
_XVNC_HINT = (
    "odda needs TigerVNC's Xvnc for its per-browser virtual displays; "
    "on Debian/Ubuntu: apt install tigervnc-standalone-server"
)
_WEBSOCKIFY_HINT = (
    "odda needs websockify to serve its per-browser noVNC endpoints; "
    "it is a pip dependency of odda — reinstall odda with dependencies"
)


class VirtualDisplayError(RuntimeError):
    pass


# See ADR-0040.
if sys.platform == "linux":
    _LIBC = ctypes.CDLL(None, use_errno=True)
    _PR_SET_PDEATHSIG = 1

    def _die_with_parent(parent_pid: int) -> None:
        _LIBC.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
        if os.getppid() != parent_pid:
            os.kill(os.getpid(), signal.SIGTERM)

else:

    def _die_with_parent(parent_pid: int) -> None:
        del parent_pid


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _reap_stale_display(display_num: int) -> bool:
    lock = Path(f"/tmp/.X{display_num}-lock")  # noqa: S108 - X11 lock
    x_socket = _X11_UNIX / f"X{display_num}"
    if not lock.exists():
        return not x_socket.exists()
    try:
        owner = int(lock.read_text().strip())
    except (OSError, ValueError):
        return False
    if _process_alive(owner):
        return False
    lock.unlink(missing_ok=True)
    x_socket.unlink(missing_ok=True)
    return True


class VirtualDisplay:
    # See ADR-0040.

    def __init__(
        self,
        xvnc: subprocess.Popen[Any],
        websockify: subprocess.Popen[Any],
        display: str,
        rfb_port: int,
        http_port: int,
        bind: str,
    ) -> None:
        self._xvnc = xvnc
        self._websockify = websockify
        self.display = display
        self.rfb_port = rfb_port
        self.http_port = http_port
        self.bind = bind

    @property
    def vnc_url(self) -> str:
        return f"vnc://127.0.0.1:{self.rfb_port}"

    @property
    def novnc_url(self) -> str:
        return f"http://{_bind_host(self.bind)}:{self.http_port}/vnc.html"

    @property
    def env(self) -> dict[str, str]:
        return {"DISPLAY": self.display}

    async def close(self) -> None:
        for process in (self._xvnc, self._websockify):
            if process.poll() is not None:
                continue
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
        for process in (self._xvnc, self._websockify):
            if process.poll() is not None:
                continue
            try:
                await asyncio.to_thread(process.wait, _STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await asyncio.to_thread(process.wait, _STOP_TIMEOUT_S)


def _bind_host(bind: str) -> str:
    if bind not in ("0.0.0.0", "::"):  # noqa: S104 - compared, never bound
        return bind
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return str(s.getsockname()[0])
    except OSError:
        return bind


def _tcp_ready(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _rfb_banner_ok(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5) as s:
            s.settimeout(0.5)
            return s.recv(12).startswith(b"RFB ")
    except OSError:
        return False


def _x_socket_ready(display_num: int) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(str(_X11_UNIX / f"X{display_num}"))
    except OSError:
        return False
    return True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _stderr_tail(err_file: Any) -> str:
    try:
        err_file.seek(0)
        return err_file.read().decode(errors="replace").strip()[-400:]
    except OSError:
        return ""


def _spawn_xvnc(display_num: int, port: int, width: int, height: int):
    xvnc = shutil.which("Xvnc")
    if xvnc is None:
        raise VirtualDisplayError(_XVNC_HINT)
    _X11_UNIX.mkdir(parents=True, exist_ok=True)
    err_file = tempfile.TemporaryFile()  # noqa: SIM115 - outlives the child
    parent_pid = os.getpid()
    process = subprocess.Popen(  # noqa: S603
        [
            xvnc,
            f":{display_num}",
            "-geometry",
            f"{width}x{height}",
            "-depth",
            "24",
            "-rfbport",
            str(port),
            "-localhost",
            "-SecurityTypes",
            "None",
            "-ac",
            "-extension",
            "RANDR",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=err_file,
        start_new_session=True,
        preexec_fn=lambda: _die_with_parent(parent_pid),  # noqa: PLW1509
    )
    return process, err_file


def _spawn_websockify(bind: str, http_port: int, rfb_port: int):
    if importlib.util.find_spec("websockify") is None:
        raise VirtualDisplayError(_WEBSOCKIFY_HINT)
    err_file = tempfile.TemporaryFile()  # noqa: SIM115 - outlives the child
    parent_pid = os.getpid()
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "websockify",
            "--web",
            str(_NOVNC_DIR),
            f"{bind}:{http_port}",
            f"127.0.0.1:{rfb_port}",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=err_file,
        start_new_session=True,
        preexec_fn=lambda: _die_with_parent(parent_pid),  # noqa: PLW1509
    )
    return process, err_file


def _terminate(*processes: subprocess.Popen[Any]) -> None:
    for process in processes:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with suppress(subprocess.TimeoutExpired):
            process.wait(_STOP_TIMEOUT_S)


async def start_virtual_display(width: int, height: int, bind: str) -> VirtualDisplay:
    if threading.current_thread() is not threading.main_thread():
        raise VirtualDisplayError(
            "virtual displays must be started on the main thread: their "
            "parent-death teardown keys on the spawning thread, and only "
            "the main thread is guaranteed to outlive the process"
        )
    xvnc_err: Any = None
    ws_err: Any = None
    for display_num in random.sample(list(_DISPLAY_RANGE), _DISPLAY_TRIES):
        if not _reap_stale_display(display_num):
            continue
        rfb_port = _free_port()
        http_port = _free_port()
        xvnc, xvnc_err = _spawn_xvnc(display_num, rfb_port, width, height)
        websockify, ws_err = _spawn_websockify(bind, http_port, rfb_port)
        deadline = time.monotonic() + _START_TIMEOUT_S
        while time.monotonic() < deadline:
            if xvnc.poll() is not None or websockify.poll() is not None:
                break
            checks = (
                await asyncio.to_thread(_x_socket_ready, display_num),
                await asyncio.to_thread(_rfb_banner_ok, rfb_port),
                await asyncio.to_thread(_tcp_ready, _bind_host(bind), http_port),
            )
            if all(checks):
                return VirtualDisplay(
                    xvnc, websockify, f":{display_num}", rfb_port, http_port, bind
                )
            await asyncio.sleep(0.05)
        _terminate(xvnc, websockify)
    tail = _stderr_tail(xvnc_err) or _stderr_tail(ws_err)
    detail = f": {tail}" if tail else " (no free display number)"
    raise VirtualDisplayError(f"virtual display failed to start{detail}")
