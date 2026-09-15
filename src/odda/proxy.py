"""Proxy server wrapping mitmproxy with asyncio.create_task."""

from __future__ import annotations

import asyncio
import socket
import time
from contextlib import suppress
from typing import Any

from mitmproxy import exceptions as mitmproxy_exceptions
from mitmproxy.options import Options
from mitmproxy.proxy import mode_specs
from mitmproxy.tools.dump import DumpMaster

from odda.flowstore import FlowFileAddon
from odda.proxyscript import ProxyScriptManager

# Returned by set_upstream/clear_upstream: a flip rebinds the listener
# and then closes live client connections, so the new vantage applies
# to the very next request a client dials (in-flight requests on the
# closed sockets are aborted).
_FLIP_NOTE = (
    "open connections were closed so the new path applies immediately; "
    "in-flight requests on them are aborted"
)

_FLIP_NOTE_DEGRADED = (
    "existing connections could not be closed (connection registry "
    "unavailable); the new path applies to new connections only"
)

# _await_listener_rebound's deadline; generous — the rebind is a local
# stop-then-start, normally sub-100ms.
_REBIND_TIMEOUT = 5.0


class ProxyServer:
    """Manages mitmproxy server lifecycle - auto-starts on init without blocking."""

    def __init__(
        self,
        host: str = "127.0.0.1",
    ) -> None:
        """Initialize proxy server.

        Args:
            host: Host to bind to.
        """
        self.m = None
        # Upstream-proxy state: None = direct egress (the default).
        # Changed at runtime via set_upstream/clear_upstream — the
        # configuration surface is per-session only, never persisted.
        self._upstream_url: str | None = None
        self._upstream_auth: str | None = None
        port = self._pick_port(host)
        self.options = Options(
            listen_port=port,
            listen_host=host,
        )
        # odda's audience tests against staging boxes, internal hosts, and
        # appliances with self-signed/expired certs. Skipping upstream TLS
        # validation lets the proxy forward to those hosts without 502s.
        # See docs/adr/0007-proxy-ssl-insecure.md.
        self.options.ssl_insecure = True
        self.m = DumpMaster(self.options, with_termlog=False, with_dumper=False)
        # DumpMaster ships mitmproxy's ErrorCheck addon: any ERROR-level
        # log record during its startup checkpoints sys.exit(1)s the
        # process. That is CLI UX (a human watching a console); odda is a
        # library embedding DumpMaster, and the MCP process is an agent's
        # session — a broken persisted proxy-script (logged, then skipped
        # per its contract) or any stray startup ERROR must not kill the
        # session. Disarm it: out of the addon chain (no checkpoints) and
        # its global log handler uninstalled.
        errorcheck = self.m.addons.get("errorcheck")
        if errorcheck is not None:
            self.m.addons.remove(errorcheck)
            errorcheck.finish()
        # Real-world servers send non-conformant HTTP/2 header values (e.g.
        # ` IE=Edge`); the h2 library rejects those and mitmproxy 502s. Skip
        # inbound header validation to accept them. Set after DumpMaster
        # construction: the option is registered by the proxyserver addon
        # during init, so it is unknown before then.
        # See docs/adr/0012-proxy-skip-inbound-header-validation.md.
        self.options.validate_inbound_headers = False
        # Set client replay concurrency to -1 (no limit) so queue.join()
        # returns after request is dispatched, not after response received.
        self.m.options.client_replay_concurrency = -1

        # Initialize flow storage addon
        self.db_addon = FlowFileAddon()
        self.m.addons.add(self.db_addon)

        # Proxy-script manager: holds user-supplied mitmproxy addons.
        # Constructed after FlowFileAddon so FlowFileAddon stays ahead of
        # user proxy-scripts in the chain — captured .odda/flows/<id>/
        # files record the original request/response and a proxy-script's
        # mutations affect what goes upstream, not what is captured
        # (ADR-0018).
        self.scripts = ProxyScriptManager(self.m)

        self.task = asyncio.create_task(self.m.run())

    @staticmethod
    def _pick_port(host: str) -> int:
        """Bind a socket to get an OS-assigned free port.

        Avoids the TOCTOU race in check-then-bind patterns: we bind a
        socket, read the assigned port, and close it immediately before
        passing it to mitmproxy. The close→bind window is much smaller
        than scanning a range of ports, and in practice two servers
        starting in parallel get different ports from the OS.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, 0))
            return s.getsockname()[1]

    async def shutdown(self) -> None:
        """Shut down the proxy server and wait for the task to finish."""
        self.m.shutdown()
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task

    @property
    def proxy_url(self) -> str:
        """Return the proxy URL for browser configuration.

        Returns:
            HTTP proxy URL string.
        """
        return f"http://{self.options.listen_host}:{self.options.listen_port}"

    # --- upstream proxy ------------------------------------------------

    async def set_upstream(self, url: str, auth: str | None = None) -> dict[str, Any]:
        """Chain all server-side traffic through an upstream HTTP(S) proxy.

        Swaps the single listener's mitmproxy mode to ``upstream:<url>``
        (pinned to odda's own listen host/port via the mode-spec ``@``
        suffix) and sets ``upstream_auth`` (HTTP Basic) before the flip,
        so no unauthenticated CONNECT reaches the upstream. mitmproxy
        validates synchronously on assignment: a bad spec raises
        ``OptionsError`` and rolls the options back — odda converts it
        to ``ValueError`` (the tool layer's anticipated-error type).
        The listener rebind is asynchronous, so this awaits it before
        returning, then closes live client connections so the flip
        converges on the client's very next request.

        URL userinfo (``user:pass@host``) is rejected: mitmproxy's
        server-spec grammar has no credentials field — auth belongs in
        the ``auth`` parameter. Only ``http://`` and ``https://``
        upstreams exist; there is no SOCKS support to fall back to.

        Args:
            url: Upstream proxy URL (``http://`` or ``https://`` host:port).
            auth: Optional ``username:password`` for HTTP Basic auth.

        Returns:
            The new upstream state dict (see :meth:`upstream_state`) plus
            ``closed_connections`` and a ``note``: open connections are
            closed so the flip applies immediately (in-flight requests
            abort); the note flags degradation when the drain could not
            inspect the connection registry.
        """
        if "@" in url:
            raise ValueError(
                "upstream URL must not embed credentials (user:pass@host) — "
                "pass them via the auth parameter as 'username:password'"
            )
        if "://" not in url:
            raise ValueError(
                f"upstream URL {url!r} must include a scheme: only "
                "http:// and https:// upstreams are supported (no SOCKS)"
            )
        scheme = url.split("://", 1)[0].lower()
        if scheme not in ("http", "https"):
            raise ValueError(
                f"unsupported upstream proxy scheme {scheme!r}: only "
                "http:// and https:// upstreams are supported (no SOCKS)"
            )
        if auth is not None and ":" not in auth:
            raise ValueError("auth must be 'username:password'")
        spec = f"upstream:{url}@{self.options.listen_host}:{self.options.listen_port}"
        try:
            mode_specs.ProxyMode.parse(spec)
        except (ValueError, mitmproxy_exceptions.OptionsError) as exc:
            raise ValueError(f"invalid upstream proxy URL {url!r}: {exc}") from exc
        return await self._flip_upstream(spec, auth, url)

    async def clear_upstream(self) -> dict[str, Any]:
        """Drop the upstream chain and return to direct egress.

        Swaps the listener back to regular mode and clears
        ``upstream_auth`` via the shared flip procedure.
        """
        return await self._flip_upstream("regular", None, None)

    def upstream_state(self) -> dict[str, Any]:
        """Report the current upstream configuration.

        Returns:
            ``upstream``: the configured upstream URL, or ``None`` when
            direct. ``auth_set``: whether credentials are configured —
            the secret itself is never echoed back.
        """
        return {
            "upstream": self._upstream_url,
            "auth_set": self._upstream_auth is not None,
        }

    async def _flip_upstream(
        self, spec: str, auth: str | None, url: str | None
    ) -> dict[str, Any]:
        """Shared flip procedure: apply, rebind, drain, commit.

        Snapshots the current options, assigns ``upstream_auth`` then
        ``mode`` (auth first, and unconditionally — a re-set without
        auth must clear stale credentials), restores the snapshot on
        any failure so the options never half-apply, then drains live
        client connections and commits the mirror state.

        Args:
            spec: Complete mode spec to swap the listener to.
            auth: Upstream auth to set (``None`` clears stale credentials).
            url: Upstream URL to record in state (``None`` = direct).

        Returns:
            The state dict (see :meth:`upstream_state`) plus the flip
            ``note``, and ``closed_connections`` unless the drain could
            not inspect the connection registry.
        """
        previous_mode = list(self.options.mode)
        previous_auth: str | None = self.options.upstream_auth
        try:
            self.options.upstream_auth = auth
            self.options.mode = [spec]
        except mitmproxy_exceptions.OptionsError as exc:
            self._restore_options(previous_mode, previous_auth)
            raise ValueError(f"invalid upstream proxy configuration: {exc}") from exc
        try:
            await self._await_listener_rebound()
        except TimeoutError as exc:
            # The swap may actually be live — restore the previous mode so
            # upstream_state() never lies about what the listener is doing.
            self._restore_options(previous_mode, previous_auth)
            raise ValueError(str(exc)) from exc
        closed = self._drain_client_connections()
        self._upstream_url = url
        self._upstream_auth = auth
        result = {
            **self.upstream_state(),
            "note": _FLIP_NOTE if closed is not None else _FLIP_NOTE_DEGRADED,
        }
        if closed is not None:
            result["closed_connections"] = closed
        return result

    def _drain_client_connections(self) -> int | None:
        """Close every live client connection so a mode flip converges now.

        The mode is stamped onto a connection at accept time, and
        browsers pool connections (HTTP/2 especially) for minutes — a
        flip that only affects future accepts would leave revisited
        hosts on the old path indefinitely. Closing the client sockets
        makes the client re-dial into the new mode on its next request;
        Chrome reconnects transparently.

        Walks the proxyserver addon's live-handler registry (the same
        state its ``proxyserver.active_connections`` command reports)
        and closes each transport writer. Everything is reached via
        ``getattr``: if a future mitmproxy changes the shape, the flip
        degrades to affecting new connections only instead of crashing.

        Returns:
            Number of live writers closed, or ``None`` when the registry
            could not be inspected (callers flag the degradation).
        """
        proxyserver = self.m.addons.get("proxyserver")
        if proxyserver is None or not hasattr(proxyserver, "connections"):
            return None
        connections = proxyserver.connections
        closed = 0
        for handler in list(connections.values()):
            transports = getattr(handler, "transports", {})
            for io in list(transports.values()):
                writer = getattr(io, "writer", None)
                if writer is None:
                    continue
                is_closing = getattr(writer, "is_closing", None)
                if callable(is_closing) and is_closing():
                    continue
                writer.close()
                closed += 1
        return closed

    def _restore_options(self, mode: list[str], auth: str | None) -> None:
        """Best-effort restore after a failed flip so state matches egress."""
        with suppress(mitmproxy_exceptions.OptionsError):
            self.options.mode = mode
            self.options.upstream_auth = auth

    async def _await_listener_rebound(self) -> None:
        """Wait until the listener socket answers again after a mode swap.

        ``Servers.update`` rebuilds listeners as a loop task (stops
        before starts — the port is briefly unbound); callers must not
        race that window. The probe connection carries no data and
        creates no flow.
        """
        deadline = time.monotonic() + _REBIND_TIMEOUT
        last: OSError | None = None
        while time.monotonic() < deadline:
            try:
                _, writer = await asyncio.open_connection(
                    self.options.listen_host, self.options.listen_port
                )
            except OSError as exc:
                last = exc
                await asyncio.sleep(0.02)
            else:
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
                return
        raise TimeoutError(
            f"proxy listener on {self.options.listen_host}:"
            f"{self.options.listen_port} did not come back after the mode "
            f"swap within {_REBIND_TIMEOUT}s: {last}"
        )

    # --- proxy-scripts ------------------------------------------------

    def install_script(self, name: str, source: str) -> dict[str, Any]:
        """Persist, exec, and add a proxy-script under ``name``.

        Overwrites an existing proxy-script of the same name (remove-then-add),
        which is the ``--force`` path; the caller gates whether overwrite
        is allowed.

        Raises:
            ValueError: If exec fails (syntax/import error). The persisted
                file is left on disk for the boot scan to retry.
        """
        return self.scripts.install(name, source)

    def remove_script(self, name: str) -> dict[str, Any]:
        """Remove a proxy-script: live instance + on-disk source.

        Raises:
            ValueError: If the proxy-script is not on disk.
        """
        return self.scripts.remove(name)

    def list_scripts(self) -> list[dict[str, Any]]:
        """List proxy-scripts from disk, annotated with whether they're live."""
        return self.scripts.list_live()

    def restore_scripts_on_boot(self) -> None:
        """Re-exec and add every persisted proxy-script.

        Called during server boot, after :func:`flowstore.set_data_dir`.
        A proxy-script that fails to exec is logged and skipped; the rest
        of the chain comes up.
        """
        self.scripts.restore_on_boot()
