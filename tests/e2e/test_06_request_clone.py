"""Port of scrut 06-request-clone.md: request_clone copies a captured flow
into an editable request; a name collision refuses without force."""

from __future__ import annotations

from tests.e2e.conftest import curl, dyn_server


async def test_request_clone_writes_request_and_meta(odda_session) -> None:
    """Cloning a captured curl flow returns the flow's identity and writes a
    non-empty request + meta.json under the new name."""
    async with odda_session() as h, dyn_server() as dyn:
        proxy = await h.call("proxy_url", {})
        url = (
            f"{dyn.tls_base}/a?body=clone-test&status=200"
            "&header=Content-Type:application/json"
        )
        r = await curl(
            url, proxy, extra=["-o", "/dev/null", "-w", "curl_status=%{http_code}"]
        )
        assert "curl_status=200" in r.stdout

        rec = await h.wait_flow("clone-test")
        flow_id = rec["id"]

        sc = await h.call("request_clone", {"flow_id": flow_id, "name": "clone-test"})
        assert isinstance(sc, dict)
        assert sc["flow_id"] == flow_id
        assert sc["scheme"] == "https"
        assert sc["host"] == "127.0.0.1"

        d = h.data_dir / "requests" / "clone-test"
        assert (d / "request").stat().st_size > 0
        assert (d / "meta.json").stat().st_size > 0


async def test_request_clone_collision_then_force_overwrites(odda_session) -> None:
    """A second clone on an existing name is a tool error naming the collision;
    force=True overwrites the existing request."""
    async with odda_session() as h, dyn_server() as dyn:
        proxy = await h.call("proxy_url", {})
        url = (
            f"{dyn.tls_base}/a?body=clone-force&status=200"
            "&header=Content-Type:application/json"
        )
        r = await curl(
            url, proxy, extra=["-o", "/dev/null", "-w", "curl_status=%{http_code}"]
        )
        assert "curl_status=200" in r.stdout
        rec = await h.wait_flow("clone-force")
        flow_id = rec["id"]

        await h.call("request_clone", {"flow_id": flow_id, "name": "clone-force"})
        err = await h.call_error(
            "request_clone", {"flow_id": flow_id, "name": "clone-force"}
        )
        assert "already exists" in err

        sc = await h.call(
            "request_clone", {"flow_id": flow_id, "name": "clone-force", "force": True}
        )
        assert sc["name"] == "clone-force"
