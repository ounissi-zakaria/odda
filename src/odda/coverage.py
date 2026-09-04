"""Block-level code coverage via the CDP Profiler domain.

Wraps the four CDP ``Profiler`` calls used by the coverage tools:

- ``Profiler.enable`` — turn on the Profiler domain (required before
  any other Profiler command).
- ``Profiler.start`` — begin the recording window.
- ``Profiler.startPreciseCoverage({callCount: true, detailed: true})``
  turn on precise block-level coverage with per-block call counts.
- ``Profiler.takePreciseCoverage`` — read per-script, per-block hit
  counts. **Resets the counters on each read**, so each call returns
  the delta since the previous take, not a cumulative total.
- ``Profiler.stop`` — end the recording window.

Because ``takePreciseCoverage`` returns deltas, the coverage tools
accumulate counts session-side so ``stop`` can return the cumulative
counts for the whole recording window (per the PRD: "Coverage returns
raw counts for the recording window; the agent slices as needed").
``snapshot`` returns the raw delta since the previous take, so the
agent can slice a sub-window by subtracting two snapshots or by
subtracting a snapshot from the final ``stop``.

Per ADR-0005, coverage is a windowed query that spans navigations:
the recording flag, accumulator, and CDP Profiler domain all survive
main-frame navigation, so an agent can ``start`` → ``navigate`` →
``snapshot``/``stop`` to observe code that runs as a consequence of
navigating. Coverage does not survive tab close. Coverage output
always includes zero-hit blocks: the negative space is as informative
as the positive.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from patchright.async_api import CDPSession


def _format_coverage(raw: dict[str, Any], script_map: dict[str, str]) -> dict[str, Any]:
    """Shape CDP ``takePreciseCoverage`` output into the public form.

    The CDP response is ``{result: [{scriptId, url, functions:
    [{functionName, ranges: [{startOffset, endOffset, count,
    isBlock}]}]}]}``. We resolve ``url`` from the caller's
    ``script_map`` when the CDP event did not carry one (the script map
    is populated by ``Debugger.scriptParsed`` events and is the source
    of truth for script URLs in odda), keep every function and every
    block range (including zero-hit blocks), and drop the per-range
    ``isBlock`` flag — every range in ``detailed: true`` output is a
    block.

    Args:
        raw: The decoded CDP ``Profiler.takePreciseCoverage`` response.
        script_map: ``{scriptId: url}`` from the tab's script map.

    Returns:
        ``{"scripts": [{"url": str | None, "functions": [{"name":
        str | None, "ranges": [{"startOffset": int, "endOffset":
        int, "count": int}]}]}]}``.
    """
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
    """Return a fresh coverage accumulator.

    The accumulator is an opaque, mutable dict that ``merge_delta``
    grows and ``format_accumulated`` renders. Callers reset it on
    ``coverage start``; it survives navigation (per ADR-0005) and is
    cleared on tab close.

    Returns:
        An empty accumulator ``{"scripts": {url: {"functions": {
        fn_name: {(start, end): count}}}}}``. ``url`` and ``fn_name``
        are ``None``-able keys (inline scripts and anonymous functions
        are keyed under ``None`` so they merge across takes).
    """
    return {"scripts": {}}


def merge_delta(accumulator: dict[str, Any], delta: dict[str, Any]) -> None:
    """Merge a delta (shaped coverage output) into the accumulator.

    Counts are summed per ``(url, function name, range)`` so that
    repeated takes accumulate into the window total. Zero-hit blocks
    in a delta do not change the accumulator (adding zero), but they
    do establish the range key so a later non-zero take is recorded
    against the same range.

    Args:
        accumulator: The accumulator (mutated in place).
        delta: A shaped coverage object (as returned by
            ``_format_coverage`` / ``take_precise_coverage``).
    """
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
    """Render the accumulator as the public coverage output shape.

    Scripts are returned in insertion order (the order they first
    appeared in a delta); functions and ranges likewise. The output
    shape matches ``_format_coverage`` so ``snapshot`` and ``stop``
    return the same structure.

    Args:
        accumulator: The accumulator built by ``merge_delta``.

    Returns:
        The shaped coverage object.
    """
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
    """Enable the CDP Profiler domain and precise block-level coverage.

    ``Profiler.enable`` turns on the domain (required before any other
    Profiler command), ``Profiler.start`` begins the recording window,
    and ``Profiler.startPreciseCoverage`` turns on block-level coverage
    with per-block call counts. All three must be called before
    ``take_precise_coverage``; the order is fixed by the CDP contract.

    Args:
        cdp: The tab's CDP session.
    """
    await cdp.send("Profiler.enable")
    await cdp.send("Profiler.start")
    await cdp.send(
        "Profiler.startPreciseCoverage",
        {"callCount": True, "detailed": True},
    )


async def take_precise_coverage(
    cdp: CDPSession, script_map: dict[str, str]
) -> dict[str, Any]:
    """Read per-script, per-block hit counts (the delta since the last take).

    CDP ``Profiler.takePreciseCoverage`` resets its counters on each
    read, so this returns the delta since the previous take (or since
    ``start`` if this is the first take), not a cumulative total.
    Zero-hit blocks are included. Script URLs are resolved from
    ``script_map`` when the CDP event did not carry one.

    Args:
        cdp: The tab's CDP session (must already have Profiler enabled
            and precise coverage started).
        script_map: ``{scriptId: url}`` from the tab's script map.

    Returns:
        The shaped coverage object (see ``_format_coverage``).
    """
    raw = await cdp.send("Profiler.takePreciseCoverage")
    return _format_coverage(raw, script_map)


async def stop(cdp: CDPSession) -> None:
    """Stop precise coverage and end the Profiler recording window.

    ``Profiler.stopPreciseCoverage`` releases the coverage counters,
    ``Profiler.stop`` ends the recording window, and ``Profiler.disable``
    tears down the domain so the session is left clean. Errors from
    ``stopPreciseCoverage``/``disable`` are tolerated — the domain may
    already be torn down (e.g. the tab closed mid-stop) — but ``stop``
    errors propagate.

    Args:
        cdp: The tab's CDP session.
    """
    with suppress(Exception):
        await cdp.send("Profiler.stopPreciseCoverage")
    await cdp.send("Profiler.stop")
    with suppress(Exception):
        await cdp.send("Profiler.disable")
