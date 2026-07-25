"""Text renderers for odda CLI commands.

Each renderer turns a structured result (the JSON-RPC return value) into
human-readable text. Renderers are keyed by command name in
:data:`RENDERERS`. The CLI dispatches to the matching renderer in text
mode; ``--json`` mode bypasses renderers and prints the raw value.

Text output is free to drift for readability; it is not a parse target.
Structured (``--json``) output is the stable parse target.

Most commands render as one of three trivial shapes — a flat dict
(``key: value`` lines), a list of dicts (an aligned table), or nested
records (YAML-ish blocks). Those are declared as data in the registry:
``("kv", ["status", "ref"])``, ``("table", ["name", "type"], "(no wraps)")``,
``("records",)``. The handful of commands with real formatting logic
(``eval``, ``coverage``, ``version``, etc.) register a callable instead.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal, NamedTuple

# A renderer is either a callable (for the ~6 commands with custom logic)
# or a spec naming one of the trivial shapes. The spec types below give
# static checkers something to hold so a typo in the registry fails at
# import, not at dispatch.

_Kind = Literal["kv", "table", "records", "passthrough"]


class KvSpec(NamedTuple):
    """Flat dict as ``key: value`` lines."""

    keys: list[str]


class TableSpec(NamedTuple):
    """List of dicts as an aligned table; ``empty_msg`` when the list is empty."""

    cols: list[str]
    empty_msg: str


class RecordsSpec(NamedTuple):
    """List of records as YAML-ish blocks separated by ``---``."""


class PassthroughSpec(NamedTuple):
    """Print the value as-is (strings bare, non-strings as JSON)."""


# A trivial-shape spec is one of the NamedTuples above; a custom renderer
# is a callable. The registry values are one or the other.
Spec = KvSpec | TableSpec | RecordsSpec | PassthroughSpec
Renderer = Callable[[Any], str] | Spec


def _scalar(v: Any) -> str:
    """Format a scalar for text output.

    ``None`` prints as ``null`` and booleans as ``true``/``false`` (JSON
    convention, lowercase); everything else prints via ``str()``. Shared
    by :func:`_kv` and :func:`_yaml_scalar`.
    """
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _kv(d: dict[str, Any], *keys: str) -> str:
    """Render selected dict keys as ``key: value`` lines (missing keys skipped)."""
    return "\n".join(f"{k}: {_scalar(d[k])}" for k in keys if k in d)


def _table(rows: list[dict[str, Any]], *columns: str) -> str:
    """Render a list of dicts as an aligned table with a header+separator."""
    widths = {c: len(c) for c in columns}
    for r in rows:
        for c in columns:
            widths[c] = max(widths[c], len(str(r.get(c, ""))))
    lines = [
        "  ".join(c.ljust(widths[c]) for c in columns),
        "  ".join("-" * widths[c] for c in columns),
    ]
    lines.extend(
        "  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns) for r in rows
    )
    return "\n".join(lines)


def _yaml_scalar(v: Any) -> str:
    if v is None or isinstance(v, bool):
        return _scalar(v)
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "" or any(c in s for c in ":{}[]#&*!|>'\"%@`"):
        return json.dumps(s, ensure_ascii=False)
    return s


def _yaml_value(v: Any, indent: int = 0) -> str:
    """Render a value as a YAML-ish scalar/sequence/mapping."""
    pad = "  " * indent
    if isinstance(v, dict):
        return "\n".join(
            f"{pad}{k}:\n{_yaml_value(val, indent + 1)}"
            if isinstance(val, (dict, list))
            else f"{pad}{k}: {_yaml_scalar(val)}"
            for k, val in v.items()
        )
    if isinstance(v, list):
        return "\n".join(
            f"{pad}-\n{_yaml_value(item, indent + 1)}"
            if isinstance(item, (dict, list))
            else f"{pad}- {_yaml_scalar(item)}"
            for item in v
        )
    return f"{pad}{_yaml_scalar(v)}"


def _records(records: list[dict[str, Any]]) -> str:
    """Render a list of records as YAML-ish blocks separated by ``---``."""
    if not records:
        return "(no records)"
    return "\n".join("---\n" + _yaml_value(r, 0) for r in records)


# --- Custom renderers (the ~6 commands with real formatting logic) -----------


def r_version(d: dict[str, Any]) -> str:
    return f"odda {d['version']}"


def r_navigate(d: dict[str, Any]) -> str:
    return str(d.get("status", ""))


def r_eval(v: Any) -> str:
    # Playwright returns a parsed Python value (str/dict/list/int/bool/None),
    # not a JSON string. Render strings bare (readable, no quotes) and
    # non-strings as compact JSON. JSON.stringify inside the JS returns a
    # string, which prints raw (no double-encoding in text mode).
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


def r_page_upload(d: dict[str, Any]) -> str:
    out = _kv(d, "status", "ref")
    files = d.get("files")
    if files:
        out += "\nfiles:\n" + "\n".join(f"  - {f}" for f in files)
    return out


def r_tabs_list(rows: list[dict[str, Any]]) -> str:
    # Flatten [{browser_id, tabs:[{tab_id,url,title}]}] into a flat table.
    flat = [
        {
            "browser_id": b.get("browser_id"),
            "tab_id": t.get("tab_id"),
            "url": t.get("url", ""),
            "title": t.get("title", ""),
        }
        for b in rows
        for t in b.get("tabs", [])
    ]
    if not flat:
        return "(no tabs)"
    return _table(flat, "browser_id", "tab_id", "url", "title")


def r_coverage_start(d: dict[str, Any]) -> str:
    return str(d.get("status", ""))


def r_coverage(d: dict[str, Any]) -> str:
    # {scripts:[{url, functions:[{name, ranges:[{startOffset,endOffset,count}]}]}]}
    scripts = d.get("scripts", [])
    if not scripts:
        return "(no coverage)"
    blocks: list[str] = []
    for s in scripts:
        url = s.get("url") or "(inline)"
        functions = s.get("functions", [])
        total_hits = sum(
            r.get("count", 0) for fn in functions for r in fn.get("ranges", [])
        )
        blocks.append(f"--- {url}  (hits: {total_hits})")
        for fn in functions:
            name = fn.get("name") or "(anonymous)"
            ranges = fn.get("ranges", [])
            hit = sum(r.get("count", 0) for r in ranges)
            zero = sum(1 for r in ranges if r.get("count", 0) == 0)
            blocks.append(f"  {name}  blocks={len(ranges)} hit={hit} zero-hit={zero}")
            for r in ranges:
                blocks.append(
                    f"    [{r.get('startOffset')}-{r.get('endOffset')}] count={r.get('count')}"
                )
    return "\n".join(blocks)


def render(result: Any, spec: Renderer) -> str:
    """Dispatch a result through a renderer (callable or spec)."""
    if callable(spec):
        return spec(result)
    if isinstance(spec, KvSpec):
        return _kv(result, *spec.keys)
    if isinstance(spec, TableSpec):
        return _table(result, *spec.cols) if result else spec.empty_msg
    if isinstance(spec, RecordsSpec):
        return _records(result)
    if isinstance(spec, PassthroughSpec):
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False)
    msg = f"unknown renderer spec: {spec!r}"
    raise TypeError(msg)


# Registry: command name -> renderer (callable or spec). Command names
# match the JSON-RPC method names the CLI dispatches to (or the command's
# own name for non-RPC commands like version/install-opencode). ``logs``
# is handled inline in cli.py (streaming) and deliberately has no entry.
RENDERERS: dict[str, Renderer] = {
    "version": r_version,
    "install-opencode": KvSpec(["plugin", "skill"]),
    "status": KvSpec(
        ["socket", "data_dir", "parent_pid", "proxy_url", "browser_count"]
    ),
    "proxy/url": PassthroughSpec(),
    "browser/open": KvSpec(["browser_id", "tab_id", "status"]),
    "browser/close": KvSpec(["browser_id", "status"]),
    "navigate": r_navigate,
    "eval": r_eval,
    "wait-for": r_eval,  # same pass-through as eval
    "screenshot": PassthroughSpec(),
    "page/snapshot": PassthroughSpec(),
    "page/click": KvSpec(["status", "ref"]),
    "page/fill": KvSpec(["status", "ref"]),
    "page/hover": KvSpec(["status", "ref"]),
    "page/upload": r_page_upload,
    "tabs/list": r_tabs_list,
    "tabs/open": KvSpec(["browser_id", "tab_id", "status"]),
    "tabs/close": KvSpec(["browser_id", "tab_id", "status"]),
    "event/listeners": TableSpec(
        ["type", "element_tag", "script_url", "line_number", "column_number"],
        "(no listeners)",
    ),
    "coverage/start": r_coverage_start,
    "coverage/snapshot": r_coverage,
    "coverage/stop": r_coverage,
    "wrap/calls/add": KvSpec(
        ["name", "type", "expr", "userscript_name", "size", "extension_id"]
    ),
    "wrap/access/add": KvSpec(
        ["name", "type", "expr", "userscript_name", "size", "extension_id"]
    ),
    "wrap/list": TableSpec(["name", "type", "expr"], "(no wraps)"),
    "wrap/remove": KvSpec(["name", "removed", "extension_id"]),
    "wrap/dump": RecordsSpec(),
    "wrap/clear": KvSpec(["status", "count"]),
    "logpoint/add": KvSpec(
        ["status", "id", "cdp_id", "url", "line", "col", "expr", "warning"]
    ),
    "logpoint/list": TableSpec(["id", "url", "line", "col", "expr"], "(no logpoints)"),
    "logpoint/dump": RecordsSpec(),
    "logpoint/clear": KvSpec(["status", "count"]),
    "logpoint/remove": KvSpec(["status", "id"]),
    "userscript/install": KvSpec(["name", "size", "extension_id"]),
    "userscript/list": TableSpec(["name", "size"], "(no userscripts)"),
    "userscript/remove": KvSpec(["name", "removed", "extension_id"]),
    "request/clone": KvSpec(["name", "path", "flow_id", "scheme", "host", "port"]),
    "request/new": KvSpec(["name", "path", "scheme", "host", "port"]),
    "request/send": KvSpec(
        [
            "id",
            "method",
            "scheme",
            "host",
            "port",
            "path",
            "status_code",
            "total_duration_ms",
            "body_file",
            "error",
        ]
    ),
}
