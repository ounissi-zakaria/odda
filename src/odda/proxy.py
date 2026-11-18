from __future__ import annotations

import asyncio
import base64
import socket
import time
import weakref
from contextlib import suppress
from typing import Any

from mitmproxy import exceptions as mitmproxy_exceptions, http as mitmproxy_http
from mitmproxy.options import Options
from mitmproxy.proxy import mode_specs
from mitmproxy.tools.dump import DumpMaster

from odda.flowstore import AUTH_CHALLENGE_TAG, PROXYAUTH_METADATA_KEY, FlowFileAddon
from odda.proxyscript import ProxyScriptManager

# See ADR-0031.
AUTH_CANARY_HOST = "odda-seed.invalid"
AUTH_CANARY_URL = f"http://{AUTH_CANARY_HOST}/canary"

PROXY_AUTH_PASSWORD = "odda"  # noqa: S105

_FLIP_NOTE = (
    "open connections were closed so the new path applies immediately; "
    "in-flight requests on them are aborted"
)

_FLIP_NOTE_DEGRADED = (
    "existing connections could not be closed (connection registry "
    "unavailable); the new path applies to new connections only"
)

_REBIND_TIMEOUT = 5.0


def _parse_basic_proxy_auth(header_value: str) -> tuple[str, str] | None:
    try:
        scheme, _, blob = header_value.partition(" ")
        if scheme.lower() != "basic":
            return None
        user, _, pw = base64.b64decode(blob.strip()).decode("utf-8").partition(":")
    except ValueError:
        return None
    return (user, pw) if user else None


class BrowserAuthAddon:
    # See ADR-0031.

    def __init__(self) -> None:
        self._authenticated: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def http_connect(self, flow) -> None:
        from_mitmproxy_mode = isinstance(
            flow.client_conn.proxy_mode, mode_specs.UpstreamMode
        )
        creds = _parse_basic_proxy_auth(
            flow.request.headers.get("Proxy-Authorization", "")
        )
        flow.request.headers.pop("Proxy-Authorization", None)
        if from_mitmproxy_mode:
            return
        if creds is not None:
            flow.metadata[PROXYAUTH_METADATA_KEY] = creds
            self._authenticated[flow.client_conn] = creds

    def requestheaders(self, flow) -> None:
        if isinstance(flow.client_conn.proxy_mode, mode_specs.UpstreamMode):
            return
        creds = _parse_basic_proxy_auth(
            flow.request.headers.get("Proxy-Authorization", "")
        )
        if creds is not None:
            flow.request.headers.pop("Proxy-Authorization", None)
        else:
            creds = self._authenticated.get(flow.client_conn)
        if creds is not None:
            flow.metadata[PROXYAUTH_METADATA_KEY] = creds

        if flow.request.host == AUTH_CANARY_HOST:
            if creds is None:
                flow.metadata[AUTH_CHALLENGE_TAG] = True
                flow.response = mitmproxy_http.Response.make(
                    407,
                    b"proxy auth seeding",
                    {
                        "Proxy-Authenticate": 'Basic realm="odda"',
                        "Content-Type": "text/plain",
                    },
                )
            else:
                flow.metadata[AUTH_CHALLENGE_TAG] = True
                flow.response = mitmproxy_http.Response.make(
                    200, b"seeded", {"Content-Type": "text/plain"}
                )


class ProxyServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
    ) -> None:
        self.m = None
        self._upstream_url: str | None = None
        self._upstream_auth: str | None = None
        port = self._pick_port(host)
        self.options = Options(
            listen_port=port,
            listen_host=host,
        )
        self.options.ssl_insecure = True
        self.m = DumpMaster(self.options, with_termlog=False, with_dumper=False)
        errorcheck = self.m.addons.get("errorcheck")
        if errorcheck is not None:
            self.m.addons.remove(errorcheck)
            errorcheck.finish()
        self.options.validate_inbound_headers = False
        self.m.options.client_replay_concurrency = -1

        # See ADR-0018.
        self.db_addon = FlowFileAddon()
        self.m.addons.add(BrowserAuthAddon(), self.db_addon)

        # See ADR-0018.
        self.scripts = ProxyScriptManager(self.m)

        self.task = asyncio.create_task(self.m.run())

    @staticmethod
    def _pick_port(host: str) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, 0))
            return s.getsockname()[1]

    async def shutdown(self) -> None:
        self.m.shutdown()
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task

    @property
    def proxy_url(self) -> str:
        return f"http://{self.options.listen_host}:{self.options.listen_port}"

    async def set_upstream(self, url: str, auth: str | None = None) -> dict[str, Any]:
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
        return await self._flip_upstream("regular", None, None)

    def upstream_state(self) -> dict[str, Any]:
        return {
            "upstream": self._upstream_url,
            "auth_set": self._upstream_auth is not None,
        }

    async def _flip_upstream(
        self, spec: str, auth: str | None, url: str | None
    ) -> dict[str, Any]:
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
        with suppress(mitmproxy_exceptions.OptionsError):
            self.options.mode = mode
            self.options.upstream_auth = auth

    async def _await_listener_rebound(self) -> None:
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

    def install_script(self, name: str, source: str) -> dict[str, Any]:
        return self.scripts.install(name, source)

    def remove_script(self, name: str) -> dict[str, Any]:
        return self.scripts.remove(name)

    def list_scripts(self) -> list[dict[str, Any]]:
        return self.scripts.list_live()

    def restore_scripts_on_boot(self) -> None:
        self.scripts.restore_on_boot()
