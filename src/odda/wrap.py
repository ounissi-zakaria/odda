"""Wrap: transparent observation at a named function or property accessor.

A Wrap is a transparent wrapper installed as a named userscript via the
existing userscript mechanism. The generated JS replaces the original
function or property accessor with a wrapper that records each call or
access (receiver, arguments, return value, call stack) into
``window.__oddaWrap``, then calls through to the original. The wrapper
is leaf-only (ADR-0003): it records the call it was placed on and does
not follow callbacks passed as arguments.

Wraps reach all frames in a tab including cross-origin iframes
(inherited from the userscript extension's ``all_frames: true``).
Records are wiped on navigation (the userscript re-initializes
``window.__oddaWrap`` on each ``document_start``); installations
persist across navigation (the userscript re-runs on every load).

Per ADR-0004, records are wiped on navigation. The agent must dump
before navigating again or the records are lost.

Wrap userscripts are named ``__odda-wrap__<name>`` so they can be
distinguished from user-installed userscripts. A ``wrap-meta.json``
sidecar in the userscript directory records the wrap spec (name, type,
expr) so ``wrap list`` can return structured information without
parsing JS comments.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from odda import userscript

if TYPE_CHECKING:
    from pathlib import Path

WRAP_USERSCRIPT_PREFIX = "__odda-wrap__"

# Serialization limits (implementation details).
_MAX_DEPTH = 5
_MAX_ARRAY = 100
_MAX_OBJECT_KEYS = 50
_MAX_STRING = 10000
_MAX_FUNCTION_SOURCE = 1000


def _wrap_userscript_name(name: str) -> str:
    """Return the userscript name for a wrap named ``name``."""
    return f"{WRAP_USERSCRIPT_PREFIX}{name}"


def _wrap_meta_path(browser_id: str, userscript_name: str) -> Path:
    """Return the path to the wrap's ``wrap-meta.json`` sidecar."""
    return userscript.userscripts_dir(browser_id) / userscript_name / "wrap-meta.json"


# --- JS helpers (shared by all wrap userscripts) ---------------------------

# The shared helpers are defined once per page load by the first wrap
# userscript to run. Subsequent wraps reuse them. On navigation, the
# page is fresh so the helpers are undefined and the first wrap
# re-defines them. The helpers are:
# - ``__oddaSerialize``: serialize a JS value to a JSON-safe form,
#   converting functions to opaque refs, truncating large/cyclic values.
# - ``__oddaStack``: serialize ``new Error().stack`` into uniform frames.
# - ``__oddaWrap``: the per-tab record array, initialized to ``[]``.

_HELPERS_JS = (
    """\
if (!window.__oddaSerialize) {
  window.__oddaSerialize = function(v, depth, seen) {
    if (v === null) return null;
    if (v === undefined) return undefined;
    var t = typeof v;
    if (t === 'function') {
      try {
        var src = String(v);
        if (src.length > %MAX_FN%) src = src.substring(0, %MAX_FN%) + '...';
        return {type: 'function', name: v.name || null, source: src};
      } catch (e) {
        return {type: 'function', name: v.name || null};
      }
    }
    if (t === 'string') {
      if (v.length > %MAX_STRING%) return {type: 'string', truncated: true, length: v.length, preview: v.substring(0, %MAX_STRING%)};
      return v;
    }
    if (t === 'number' || t === 'boolean' || t === 'bigint') return v;
    if (t === 'symbol') return {type: 'symbol', description: v.description || null};
    if (t !== 'object') return String(v);
    if (seen.has(v)) return {type: 'object', truncated: true, cycle: true};
    if (depth > %MAX_DEPTH%) return {type: 'object', truncated: true, depth: true};
    var ns = new Set(seen);
    ns.add(v);
    if (Array.isArray(v)) {
      if (v.length > %MAX_ARRAY%) return {type: 'array', truncated: true, length: v.length};
      var arr = [];
      for (var i = 0; i < v.length; i++) arr.push(window.__oddaSerialize(v[i], depth + 1, ns));
      return arr;
    }
    try { if (v instanceof Node) return {type: 'node', name: v.nodeName || null, tag: v.tagName || null}; } catch (e) {}
    var keys;
    try { keys = Object.keys(v); } catch (e) { return {type: 'object', truncated: true, error: String(e)}; }
    if (keys.length > %MAX_OBJECT_KEYS%) return {type: 'object', truncated: true, keys: keys.slice(0, %MAX_OBJECT_KEYS%)};
    var obj = {};
    for (var ki = 0; ki < keys.length; ki++) {
      var k = keys[ki];
      try {
        // Use getOwnPropertyDescriptor to avoid triggering wrapped
        // accessors (e.g. document.cookie) during serialization.
        var d = Object.getOwnPropertyDescriptor(v, k);
        if (d && !('value' in d)) { obj[k] = {type: 'accessor'}; continue; }
        var cv = d ? d.value : v[k];
        obj[k] = window.__oddaSerialize(cv, depth + 1, ns);
      } catch (e) { obj[k] = {type: 'error', message: String(e)}; }
    }
    return obj;
  };
}
if (!window.__oddaStack) {
  window.__oddaStack = function() {
    var err = new Error();
    var stack = err.stack || '';
    var lines = stack.split('\\n');
    var frames = [];
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].trim();
      if (line.indexOf('at ') !== 0) continue;
      line = line.substring(3);
      var m;
      m = line.match(/^(.+?)\\s*\\((.+?):(\\d+):(\\d+)\\)$/);
      if (m) { frames.push({fn: m[1], url: m[2], line: parseInt(m[3], 10), col: parseInt(m[4], 10)}); continue; }
      m = line.match(/^(.+?):(\\d+):(\\d+)$/);
      if (m) { frames.push({fn: null, url: m[1], line: parseInt(m[2], 10), col: parseInt(m[3], 10)}); continue; }
      m = line.match(/^(.+?)\\s*\\(native\\)$/);
      if (m) { frames.push({fn: m[1], url: null, line: null, col: null}); continue; }
      m = line.match(/^(.+?)\\s*\\((.+?)\\)$/);
      if (m) { frames.push({fn: m[1], url: null, line: null, col: null}); continue; }
      frames.push({fn: null, url: null, line: null, col: null});
    }
    return frames.filter(function(f) { return !f.url || f.url.indexOf('chrome-extension://') !== 0; });
  };
}
// Records aggregate to the top frame's __oddaWrap when accessible
// (same-origin iframes), so `wrap_dump` reading the main frame
// sees records from all same-origin frames. Cross-origin iframes
// can't reach window.top, so they keep their own __oddaWrap (the
// wrap still runs there; the agent reads those records by evaluating
// in the iframe's context).
if (!window.__oddaWrapPush) {
  window.__oddaWrapPush = function(rec) {
    try {
      var top = window.top;
      if (top !== window && top.__oddaWrapPush) {
        top.__oddaWrapPush(rec);
        return;
      }
    } catch (e) { /* cross-origin: fall through to local */ }
    if (!window.__oddaWrap) window.__oddaWrap = [];
    window.__oddaWrap.push(rec);
  };
}
if (!window.__oddaWrap) window.__oddaWrap = [];
""".replace("%MAX_STRING%", str(_MAX_STRING))
    .replace("%MAX_DEPTH%", str(_MAX_DEPTH))
    .replace("%MAX_ARRAY%", str(_MAX_ARRAY))
    .replace("%MAX_OBJECT_KEYS%", str(_MAX_OBJECT_KEYS))
    .replace("%MAX_FN%", str(_MAX_FUNCTION_SOURCE))
)


# --- Call wrap generation --------------------------------------------------

# A call wrap replaces the function at ``expr`` (e.g. ``JSON.parse``,
# ``EventTarget.prototype.addEventListener``) with a wrapper that
# records each call. The ``expr`` is split on the last dot to find the
# owner object and property name; the function is replaced in place so
# the original call site still reaches it. The wrapper calls through to
# the original with ``apply(this, args)`` so ``this`` is preserved.

_CALL_WRAPPER_TEMPLATE = """\
(() => {
// odda-wrap $METADATA$
$HELPERS$
  var __oddaWrapName = $NAME_JSON$;
  var __oddaExpr = $EXPR_JSON$;
  var lastDot = __oddaExpr.lastIndexOf('.');
  var owner, prop;
  if (lastDot === -1) { owner = globalThis; prop = __oddaExpr; }
  else {
    try { owner = (0, eval)(__oddaExpr.substring(0, lastDot)); } catch (e) { return; }
    prop = __oddaExpr.substring(lastDot + 1);
  }
  var original;
  try { original = owner[prop]; } catch (e) { return; }
  if (typeof original !== 'function') return;
  var wrapped = function() {
    var args = Array.prototype.slice.call(arguments);
    var ret;
    try { ret = original.apply(this, args); }
    catch (e) {
      try {
        window.__oddaWrapPush({
          wrap: __oddaWrapName,
          type: 'call',
          this: window.__oddaSerialize(this, 0, new Set()),
          args: args.map(function(a) { return window.__oddaSerialize(a, 0, new Set()); }),
          ret: null,
          error: String(e),
          stack: window.__oddaStack()
        });
      } catch (recErr) {}
      throw e;
    }
    try {
      window.__oddaWrapPush({
        wrap: __oddaWrapName,
        type: 'call',
        this: window.__oddaSerialize(this, 0, new Set()),
        args: args.map(function(a) { return window.__oddaSerialize(a, 0, new Set()); }),
        ret: window.__oddaSerialize(ret, 0, new Set()),
        stack: window.__oddaStack()
      });
    } catch (recErr) {}
    return ret;
  };
  try { Object.defineProperty(wrapped, 'name', {value: original.name, configurable: true}); } catch (e) {}
  try { wrapped.toString = function() { return original.toString(); }; } catch (e) {}
  owner[prop] = wrapped;
})();
"""


# --- Access wrap generation ------------------------------------------------

# An access wrap replaces the property descriptor at ``expr`` (e.g.
# ``document.cookie``, ``HTMLElement.prototype.innerHTML``) with a
# wrapper that records each get and set. The ``expr`` is split on the
# last dot to find the owner; the descriptor is found by walking the
# prototype chain (the property may be inherited). Both getter and
# setter are wrapped if present; a get records ``ret`` as the value
# read, a set records ``args[0]`` as the value written with ``ret:
# null``.

_ACCESS_WRAPPER_TEMPLATE = """\
(() => {
// odda-wrap $METADATA$
$HELPERS$
  var __oddaWrapName = $NAME_JSON$;
  var __oddaExpr = $EXPR_JSON$;
  var lastDot = __oddaExpr.lastIndexOf('.');
  if (lastDot === -1) return;
  var prop = __oddaExpr.substring(lastDot + 1);
  var owner;
  try { owner = (0, eval)(__oddaExpr.substring(0, lastDot)); } catch (e) { return; }
  var descOwner = owner;
  var desc = null;
  while (descOwner) {
    try { desc = Object.getOwnPropertyDescriptor(descOwner, prop); } catch (e) { desc = null; }
    if (desc) break;
    try { descOwner = Object.getPrototypeOf(descOwner); } catch (e) { descOwner = null; }
  }
  if (!desc) return;
  var origGet = desc.get;
  var origSet = desc.set;
  var newDesc = {
    enumerable: desc.enumerable,
    configurable: desc.configurable
  };
  if (origGet) {
    newDesc.get = function() {
      var val = origGet.call(this);
      try {
        window.__oddaWrapPush({
          wrap: __oddaWrapName,
          type: 'access',
          this: window.__oddaSerialize(this, 0, new Set()),
          args: [],
          ret: window.__oddaSerialize(val, 0, new Set()),
          stack: window.__oddaStack()
        });
      } catch (e) {}
      return val;
    };
  }
  if (origSet) {
    newDesc.set = function(v) {
      try {
        window.__oddaWrapPush({
          wrap: __oddaWrapName,
          type: 'access',
          this: window.__oddaSerialize(this, 0, new Set()),
          args: [window.__oddaSerialize(v, 0, new Set())],
          ret: null,
          stack: window.__oddaStack()
        });
      } catch (e) {}
      return origSet.call(this, v);
    };
  }
  if (!origGet && !origSet) return;
  try { Object.defineProperty(descOwner, prop, newDesc); } catch (e) {}
})();
"""


def _build_wrapper(template: str, name: str, wrap_type: str, expr: str) -> str:
    """Fill in a wrapper template with the wrap spec and shared helpers."""
    metadata = json.dumps({"name": name, "type": wrap_type, "expr": expr})
    return (
        template.replace("$METADATA$", json.dumps(metadata))
        .replace("$NAME_JSON$", json.dumps(name))
        .replace("$EXPR_JSON$", json.dumps(expr))
        .replace("$HELPERS$", _HELPERS_JS)
    )


def generate_call_wrapper(name: str, expr: str) -> str:
    """Generate the wrapper JS for a function (call) wrap.

    Args:
        name: The wrap name (agent-supplied, used in records and as the
            userscript name suffix).
        expr: A JS expression resolving to the function to wrap (e.g.
            ``JSON.parse``, ``EventTarget.prototype.addEventListener``).

    Returns:
        The wrapper JS source, ready to install as a userscript.
    """
    return _build_wrapper(_CALL_WRAPPER_TEMPLATE, name, "call", expr)


def generate_access_wrapper(name: str, expr: str) -> str:
    """Generate the wrapper JS for a property accessor (access) wrap.

    Args:
        name: The wrap name.
        expr: A dotted JS path to the property (e.g.
            ``document.cookie``, ``HTMLElement.prototype.innerHTML``).
            The last segment is the property name; the preceding
            segments are the owner expression. Both getter and setter
            are wrapped if present.

    Returns:
        The wrapper JS source, ready to install as a userscript.
    """
    return _build_wrapper(_ACCESS_WRAPPER_TEMPLATE, name, "access", expr)


# --- Install / remove / list (disk state) ----------------------------------


def _write_meta(browser_id: str, userscript_name: str, meta: dict[str, Any]) -> None:
    """Write the wrap-meta.json sidecar for a wrap userscript."""
    _wrap_meta_path(browser_id, userscript_name).write_text(
        json.dumps(meta), encoding="utf-8"
    )


def _install(
    browser_id: str,
    name: str,
    wrap_type: str,
    expr: str,
    generator,
) -> dict[str, Any]:
    """Install a wrap as a named userscript and write its meta sidecar.

    Args:
        browser_id: Browser whose scope to install into.
        name: The wrap name.
        wrap_type: ``"call"`` or ``"access"``.
        expr: A JS expression/path for the wrap target.
        generator: ``generate_call_wrapper`` or
            ``generate_access_wrapper``.

    Returns:
        ``{name, type, expr, userscript_name, size}``.
    """
    us_name = _wrap_userscript_name(name)
    js = generator(name, expr)
    result = userscript.install(browser_id, us_name, js)
    meta = {"name": name, "type": wrap_type, "expr": expr}
    _write_meta(browser_id, us_name, meta)
    result.update(meta)
    result["userscript_name"] = us_name
    return result


def install_call(browser_id: str, name: str, expr: str) -> dict[str, Any]:
    """Install a call wrap as a named userscript and write its meta sidecar.

    Args:
        browser_id: Browser whose scope to install into.
        name: The wrap name.
        expr: A JS expression resolving to the function to wrap.

    Returns:
        ``{name, type, expr, userscript_name, size}``.
    """
    return _install(browser_id, name, "call", expr, generate_call_wrapper)


def install_access(browser_id: str, name: str, expr: str) -> dict[str, Any]:
    """Install an access wrap as a named userscript and write its meta sidecar.

    Args:
        browser_id: Browser whose scope to install into.
        name: The wrap name.
        expr: A dotted JS path to the property to wrap.

    Returns:
        ``{name, type, expr, userscript_name, size}``.
    """
    return _install(browser_id, name, "access", expr, generate_access_wrapper)


def remove(browser_id: str, name: str) -> dict[str, Any]:
    """Remove a wrap's userscript (and its meta sidecar).

    Args:
        browser_id: Browser whose scope to remove from.
        name: The wrap name.

    Raises:
        ValueError: If the wrap does not exist.
    """
    us_name = _wrap_userscript_name(name)
    result = userscript.remove(browser_id, us_name)
    result["name"] = name
    return result


def list_wraps(browser_id: str) -> list[dict[str, Any]]:
    """List installed wraps for one browser by scanning for ``__odda-wrap__*`` userscripts.

    Args:
        browser_id: Browser whose scope to list from.

    Returns:
        A list of ``{name, type, expr}`` dicts, sorted by name.
    """
    wraps: list[dict[str, Any]] = []
    us_dir = userscript.userscripts_dir(browser_id)
    if not us_dir.exists():
        return wraps
    for entry in sorted(us_dir.iterdir()):
        if not entry.is_dir():
            continue
        if not entry.name.startswith(WRAP_USERSCRIPT_PREFIX):
            continue
        meta_path = entry / "wrap-meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            wraps.append(
                {
                    "name": meta.get("name", entry.name[len(WRAP_USERSCRIPT_PREFIX) :]),
                    "type": meta.get("type"),
                    "expr": meta.get("expr"),
                }
            )
        except (OSError, json.JSONDecodeError):
            continue
    return wraps
