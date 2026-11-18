"""Per-browser virtual X displays, served over VNC and noVNC (ADR-0040).

Every browser is headed Chrome on a virtual display odda owns — never
the user's desktop (windows would steal focus from the human's work)
and never headless (mode removed). Each display is two processes:
TigerVNC's ``Xvnc`` (X server and VNC server in one) plus a
``websockify`` bridging WebSocket clients to it and serving the vendored
noVNC page. The endpoints odda reports per browser are the human's
window into exactly that browser — the mapping from browser id to screen
is the port, so nothing in the page needs to mark it.

Binding (ADR-0040, trusted-network posture): the noVNC endpoint binds
the operator-configured address (session config's ``vnc_bind``, set
from ``odda mcp --vnc-bind``; default loopback); the raw RFB port is
loopback-only always. No authentication anywhere — every device that
can reach the bound address can view and drive any open browser.
"""

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

# The virtual display's pinned geometry: matched by Chrome's
# ``--window-size`` (chrome_args) so the window fills the display
# exactly — no window manager exists to manage either. Deterministic
# screenshots and coordinates (ADR-0039's headless half retired with
# the mode; ADR-0040).
SCREEN_WIDTH = 1440
SCREEN_HEIGHT = 900

_X11_UNIX = Path("/tmp/.X11-unix")  # noqa: S108 - the X11 standard socket dir
# Above real-session displays (:0-:2), below the 3-digit range some
# display managers grab. Eight tries per open — a number can be owned by
# another process (concurrent sessions, a leaked display).
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
    """A virtual display could not be started.

    The message is agent-facing: a required binary is missing, or a
    display process failed to come up (its stderr tail is included).
    """


# Parent-death teardown (ADR-0040): the lifespan's ``finally`` only runs
# when the MCP process exits gracefully — a harness closing a session
# with SIGTERM (Python's default handler kills immediately, no
# ``finally``) or SIGKILL leaves the display processes orphaned,
# reparented to init, still serving. ``PR_SET_PDEATHSIG`` moves the
# reap into the kernel: the child is SIGTERMed when its creating thread
# dies. Every odda process spawns displays on its event-loop thread,
# which lives exactly as long as the process, so thread-death and
# process-death coincide here. Not inherited across ``fork`` — a
# websockify connection child survives its server by design and dies
# when its sockets close.
if sys.platform == "linux":
    # ``None``: the main executable's global symbol namespace, which
    # carries ``prctl`` on both glibc and musl — hardcoding
    # ``"libc.so.6"`` would crash this module's import on musl (Alpine).
    _LIBC = ctypes.CDLL(None, use_errno=True)
    _PR_SET_PDEATHSIG = 1

    def _die_with_parent(parent_pid: int) -> None:
        """preexec_fn: mark this child to SIGTERM with its parent."""
        _LIBC.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
        # fork→prctl race: if the parent died in between, getppid no
        # longer reports it — deliver the signal ourselves.
        if os.getppid() != parent_pid:
            os.kill(os.getpid(), signal.SIGTERM)

else:  # pragma: no cover - prctl is Linux-only; clean-path teardown only

    def _die_with_parent(parent_pid: int) -> None:
        """No-op where prctl does not exist."""
        del parent_pid


def _process_alive(pid: int) -> bool:
    """Whether a process with this pid exists (stdlib signal-0 probe).

    ``PermissionError`` means the pid is alive but owned by another
    user — treated as alive (owned) rather than guessed about.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _reap_stale_display(display_num: int) -> bool:
    """True when ``display_num`` is usable: free, or its owner is dead.

    An Xvnc killed uncleanly (SIGKILL) cannot remove its lock file, so
    the number looks taken forever. The lock holds the owner's pid:
    dead owner means a leftover, safe to reclaim — lock and socket
    unlinked, the number handed out again. A live owner (or an
    unparsable lock) still claims the number.
    """
    lock = Path(f"/tmp/.X{display_num}-lock")  # noqa: S108 - X11 lock
    x_socket = _X11_UNIX / f"X{display_num}"
    if not lock.exists():
        return not x_socket.exists()
    try:
        owner = int(lock.read_text().strip())
    except (OSError, ValueError):
        return False  # unreadable/foreign lock: stay out
    if _process_alive(owner):
        return False
    lock.unlink(missing_ok=True)
    x_socket.unlink(missing_ok=True)
    return True


class VirtualDisplay:
    """A running virtual display: one browser's ``Xvnc`` + ``websockify``.

    ``display`` is the ``DISPLAY`` value (``":42"``); ``vnc_url`` points
    at the loopback-only RFB port for a native VNC client; ``novnc_url``
    at the websockify serving noVNC, bound to the operator-configured
    address — on anything but loopback, reach it through the network the
    bind address lives on (deliberately unauthenticated, ADR-0040).
    Started by :func:`start_virtual_display`; torn down by the owning
    BrowserInstance together with the browser (Chrome needs the display
    until its own shutdown completes), or by the kernel when the
    spawning process dies (``PR_SET_PDEATHSIG``) — a session closed
    with SIGTERM/SIGKILL cannot leak its displays.
    """

    def __init__(
        self,
        xvnc: subprocess.Popen[Any],
        websockify: subprocess.Popen[Any],
        display: str,
        rfb_port: int,
        http_port: int,
        bind: str,
    ) -> None:
        """Store the two display processes and their endpoint addresses."""
        self._xvnc = xvnc
        self._websockify = websockify
        self.display = display
        self.rfb_port = rfb_port
        self.http_port = http_port
        self.bind = bind

    @property
    def vnc_url(self) -> str:
        """The loopback RFB endpoint (native VNC client; tunnel to it)."""
        return f"vnc://127.0.0.1:{self.rfb_port}"

    @property
    def novnc_url(self) -> str:
        """The noVNC endpoint; carries the operator-configured bind."""
        return f"http://{_bind_host(self.bind)}:{self.http_port}/vnc.html"

    @property
    def env(self) -> dict[str, str]:
        """The environment fragment pointing Chrome at this display."""
        return {"DISPLAY": self.display}

    async def close(self) -> None:
        """Terminate both processes. Idempotent; group-wide per process."""
        for process in (self._xvnc, self._websockify):
            if process.poll() is not None:
                continue
            with suppress(ProcessLookupError):
                # start_new_session=True made each its own group leader,
                # so its pid is the pgid and the kill cannot reach
                # anything else.
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
    """The host to report in URLs.

    A wildcard bind reports the machine's primary outbound IP
    (``0.0.0.0`` is not a connectable address).
    """
    if bind not in ("0.0.0.0", "::"):  # noqa: S104 - compared, never bound
        return bind
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # no traffic: just picks a route
            return str(s.getsockname()[0])
    except OSError:
        return bind


def _tcp_ready(host: str, port: int) -> bool:
    """True when the TCP port accepts connections."""
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _rfb_banner_ok(port: int) -> bool:
    """True when the VNC port answers with the RFB protocol banner."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5) as s:
            s.settimeout(0.5)
            return s.recv(12).startswith(b"RFB ")
    except OSError:
        return False


def _x_socket_ready(display_num: int) -> bool:
    """True when the display's X unix socket accepts connections."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(str(_X11_UNIX / f"X{display_num}"))
    except OSError:
        return False
    return True


def _free_port() -> int:
    """A currently-free localhost TCP port.

    Bind-then-close: the race window is milliseconds on localhost and a
    lost race only costs a display retry.
    """
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
    """Launch one Xvnc on ``:display_num``, RFB on a loopback port."""
    xvnc = shutil.which("Xvnc")
    if xvnc is None:
        raise VirtualDisplayError(_XVNC_HINT)
    _X11_UNIX.mkdir(parents=True, exist_ok=True)  # absent on X-less hosts
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
            # No X access control: the socket is process-private, and
            # Chrome must connect without any Xauthority dance.
            "-ac",
            # RandR off: with it, Chrome reads RandR's virtual max
            # (1920x1200) as the screen instead of the real geometry —
            # an incoherent screen-vs-framebuffer pair and a
            # fingerprint tell. Nothing here needs RandR: no WM, no
            # client-side resizing.
            "-extension",
            "RANDR",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=err_file,
        # Own session/group: teardown kills exactly this display.
        start_new_session=True,
        # Kernel-enforced teardown when no userspace path can run.
        # PLW1509 (fork-in-threads): the preexec body is one ctypes
        # prctl call plus getppid/kill — no allocation beyond prctl's
        # syscall, and subprocess has no first-class PDEATHSIG option.
        preexec_fn=lambda: _die_with_parent(parent_pid),  # noqa: PLW1509
    )
    return process, err_file


def _spawn_websockify(bind: str, http_port: int, rfb_port: int):
    """Launch websockify: serves noVNC, bridges WebSocket clients to RFB."""
    if importlib.util.find_spec("websockify") is None:
        raise VirtualDisplayError(_WEBSOCKIFY_HINT)
    err_file = tempfile.TemporaryFile()  # noqa: SIM115 - outlives the child
    # Run via the running interpreter: PATH-independent, tied to the
    # environment odda itself is installed in.
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
        # See _spawn_xvnc: same PLW1509 rationale.
        preexec_fn=lambda: _die_with_parent(parent_pid),  # noqa: PLW1509
    )
    return process, err_file


def _terminate(*processes: subprocess.Popen[Any]) -> None:
    """Synchronous best-effort kill for the retry path."""
    for process in processes:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with suppress(subprocess.TimeoutExpired):
            process.wait(_STOP_TIMEOUT_S)


async def start_virtual_display(width: int, height: int, bind: str) -> VirtualDisplay:
    """Start one display at ``width x height``: Xvnc + websockify/noVNC.

    ``bind`` is the address the noVNC endpoint binds (session config's
    ``vnc_bind``; raw VNC ports stay loopback-only). Tries several
    display numbers (another process may own one), waits until the X
    socket accepts, the RFB port speaks its banner, and the noVNC port
    accepts, and raises :class:`VirtualDisplayError` when a required
    binary is missing or a display never comes up.

    Must run on the main thread: parent-death teardown keys on the
    death of the spawning *thread* (PR_SET_PDEATHSIG semantics), and
    only the main thread is guaranteed to outlive the process — a
    display spawned on a worker thread would die mid-session when that
    thread exits.
    """
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
                break  # died: display taken or bad flags — next number
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
