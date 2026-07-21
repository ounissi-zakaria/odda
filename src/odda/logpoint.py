"""Logpoint: non-pausing observation at a source location.

A Logpoint is placed at a script URL, line, and column. odda plants a
CDP ``Debugger.setBreakpointByUrl`` whose condition evaluates the
agent-supplied expression in the paused-then-immediately-resumed
frame's scope, records the result, and returns ``false`` so the page
never pauses. The CDP logpoint persists across navigation (CDP re-binds
it to the re-loaded script); the per-tab records wipe on navigation
(per ADR-0004). Logpoints do not survive tab close (per-tab-session).

Records are pushed from the logpoint condition into
``window.__oddaLogpoint`` via ``window.__oddaLogpointPush``, both set
up at ``document_start`` by the default logpoint userscript. The
condition runs in the paused frame's scope (so it can read locals) and
has access to ``globalThis``/``window`` (so it can call
``__oddaLogpointPush``). On success the record carries
``{value, error: null}``; on a thrown expression the record carries
``{value: null, error: "..."}``.

The logpoint id is generated server-side (``lp-<n>`` per tab) and
embedded in the condition so records carry it. The CDP
``breakpointId`` is an internal implementation detail kept in the
per-tab registry for ``Debugger.removeBreakpoint``; it is not exposed
to the agent.

Scope: main frame and same-origin iframes only (CDP ``Debugger`` domain
is attached to the page session). Cross-origin iframes and worker
contexts are out of scope.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from patchright.async_api import CDPSession


def build_condition(lp_id: str, url: str, line: int, col: int, expr: str) -> str:
    """Build the non-pausing logpoint condition JS string.

    The condition is evaluated by CDP in the paused frame's scope. It
    evaluates the agent's ``expr`` (which may reference locals in the
    enclosing function), serializes the result via
    ``window.__oddaSerialize`` (set up by the default logpoint
    userscript), pushes a record into ``window.__oddaLogpoint`` via
    ``window.__oddaLogpointPush``, and returns ``false`` so the page
    never pauses. If ``expr`` throws, the record carries the error
    string (via ``String(e)``, which includes the error type, e.g.
    ``"ReferenceError: x is not defined"``) and ``value: null``.

    The ``expr`` is raw JS source embedded verbatim (it is NOT a
    string literal) — a reference to a local ``greeting`` must stay
    ``greeting``, not ``"greeting"``. The expr is agent-controlled and
    assumed to be valid JS.

    Args:
        lp_id: The server-generated logpoint id (echoed in records so
            the agent can correlate).
        url: The script URL (echoed in records).
        line: The 0-based line number (echoed in records).
        col: The 0-based column number (echoed in records).
        expr: The agent's raw JS expression to evaluate.

    Returns:
        The condition JS source string, ready to pass to
        ``Debugger.setBreakpointByUrl`` as ``condition``.
    """
    meta = json.dumps({"logpoint": lp_id, "url": url, "line": line, "col": col})
    return (
        "(function(){\n"
        f"  var __oddaMeta = {meta};\n"
        "  try {\n"
        f"    var v = ({expr});\n"
        "    __oddaMeta.value = window.__oddaSerialize\n"
        "      ? window.__oddaSerialize(v, 0, new Set())\n"
        "      : v;\n"
        "    __oddaMeta.error = null;\n"
        "  } catch (e) {\n"
        "    __oddaMeta.value = null;\n"
        "    __oddaMeta.error = String(e);\n"
        "  }\n"
        "  try { window.__oddaLogpointPush(__oddaMeta); } catch (pushErr) {}\n"
        "  return false;\n"
        "})()"
    )


def url_matches_script_map(url: str, script_map: dict[str, str]) -> bool:
    """Return True if any URL in ``script_map`` matches ``url``.

    A match is an exact URL equality, or ``url`` being the last path
    segment of a script URL (so the agent can pass just the filename,
    e.g. ``logpoint.js`` to match ``http://host/logpoint.js``). A bare
    suffix without a path boundary is NOT a match (so ``logpoint.js``
    does not match ``http://host/froglogpoint.js``). Empty script URLs
    (inline scripts with no URL) never match.

    Args:
        url: The agent-supplied target URL.
        script_map: ``{scriptId: url}`` from the tab's script map.

    Returns:
        True if at least one script URL matches.
    """
    if not url:
        return False
    for script_url in script_map.values():
        if not script_url:
            continue
        if script_url == url or script_url.endswith("/" + url):
            return True
    return False


async def add(
    cdp: CDPSession,
    lp_id: str,
    url: str,
    line: int,
    col: int,
    expr: str,
    script_map: dict[str, str],
) -> dict[str, Any]:
    """Plant a non-pausing logpoint via ``Debugger.setBreakpointByUrl``.

    The CDP logpoint re-binds to the re-loaded script on navigation, so
    the installation persists. The CDP ``breakpointId`` is returned for
    the caller to store and use with :func:`remove`.

    Args:
        cdp: The tab's CDP session (Debugger must be enabled).
        lp_id: The server-generated logpoint id, embedded in the
            condition so records carry it.
        url: The script URL to bind the logpoint to.
        line: 0-based line number.
        col: 0-based column number.
        expr: The agent's JS expression to evaluate at each hit.
        script_map: ``{scriptId: url}`` used to detect stale URLs. If
            no script URL matches ``url``, the command still succeeds
            but the returned dict includes a ``warning`` field.

    Returns:
        ``{"status": "planted", "id": lp_id, "cdp_id": <CDP
        breakpointId>, "url", "line", "col", "expr"}`` and optionally
        ``"warning"``.
    """
    condition = build_condition(lp_id, url, line, col, expr)
    resp = await cdp.send(
        "Debugger.setBreakpointByUrl",
        {
            "url": url,
            "lineNumber": line,
            "columnNumber": col,
            "condition": condition,
        },
    )
    cdp_id = resp.get("breakpointId", "")
    # The cdp_id is an internal implementation detail used by the
    # caller for ``Debugger.removeBreakpoint``; it is not exposed to
    # the agent (the agent uses the server-generated ``lp_id``).
    result: dict[str, Any] = {
        "status": "planted",
        "id": lp_id,
        "cdp_id": cdp_id,
        "url": url,
        "line": line,
        "col": col,
        "expr": expr,
    }
    if not url_matches_script_map(url, script_map):
        result["warning"] = (
            f"No loaded script matches url {url!r}; the logpoint will "
            "not record until a script at that URL is loaded."
        )
    return result


async def remove(cdp: CDPSession, cdp_id: str) -> dict[str, Any]:
    """Remove a logpoint's CDP logpoint.

    Args:
        cdp: The tab's CDP session.
        cdp_id: The CDP ``breakpointId`` returned by :func:`add`.

    Returns:
        ``{"status": "removed", "cdp_id": cdp_id}``.
    """
    await cdp.send("Debugger.removeBreakpoint", {"breakpointId": cdp_id})
    return {"status": "removed", "cdp_id": cdp_id}
