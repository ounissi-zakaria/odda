# See ADR-0005.
from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from patchright.async_api import CDPSession


def _format_coverage(raw: dict[str, Any], script_map: dict[str, str]) -> dict[str, Any]:
    scripts: list[dict[str, Any]] = []
    for entry in raw.get("result", []):
        script_id = entry.get("scriptId", "")
        url = entry.get("url") or script_map.get(script_id) or None
        functions: list[dict[str, Any]] = []
        for fn in entry.get("functions", []):
            ranges = [
                {
                    "startOffset": r.get("startOffset", 0),
                    "endOffset": r.get("endOffset", 0),
                    "count": r.get("count", 0),
                }
                for r in fn.get("ranges", [])
            ]
            functions.append({"name": fn.get("functionName") or None, "ranges": ranges})
        scripts.append({"url": url, "functions": functions})
    return {"scripts": scripts}


def new_accumulator() -> dict[str, Any]:
    # See ADR-0005.
    return {"scripts": {}}


def merge_delta(accumulator: dict[str, Any], delta: dict[str, Any]) -> None:
    scripts = accumulator["scripts"]
    for entry in delta.get("scripts", []):
        url = entry.get("url")
        script_acc = scripts.setdefault(url, {"functions": {}})
        functions = script_acc["functions"]
        for fn in entry.get("functions", []):
            name = fn.get("name")
            fn_acc = functions.setdefault(name, {})
            for r in fn.get("ranges", []):
                key = (r["startOffset"], r["endOffset"])
                fn_acc[key] = fn_acc.get(key, 0) + r["count"]


def format_accumulated(accumulator: dict[str, Any]) -> dict[str, Any]:
    scripts: list[dict[str, Any]] = []
    for url, script_acc in accumulator["scripts"].items():
        functions: list[dict[str, Any]] = []
        for name, fn_acc in script_acc["functions"].items():
            ranges = [
                {"startOffset": start, "endOffset": end, "count": count}
                for (start, end), count in fn_acc.items()
            ]
            functions.append({"name": name, "ranges": ranges})
        scripts.append({"url": url, "functions": functions})
    return {"scripts": scripts}


async def start(cdp: CDPSession) -> None:
    await cdp.send("Profiler.enable")
    await cdp.send("Profiler.start")
    await cdp.send(
        "Profiler.startPreciseCoverage",
        {"callCount": True, "detailed": True},
    )


async def take_precise_coverage(
    cdp: CDPSession, script_map: dict[str, str]
) -> dict[str, Any]:
    raw = await cdp.send("Profiler.takePreciseCoverage")
    return _format_coverage(raw, script_map)


async def stop(cdp: CDPSession) -> None:
    with suppress(Exception):
        await cdp.send("Profiler.stopPreciseCoverage")
    await cdp.send("Profiler.stop")
    with suppress(Exception):
        await cdp.send("Profiler.disable")
