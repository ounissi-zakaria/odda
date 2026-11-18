# See ADR-0004.
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from patchright.async_api import CDPSession


def build_condition(lp_id: str, url: str, line: int, col: int, expr: str) -> str:
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
    await cdp.send("Debugger.removeBreakpoint", {"breakpointId": cdp_id})
    return {"status": "removed", "cdp_id": cdp_id}
