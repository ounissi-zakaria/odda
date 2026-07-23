// odda logpoint helpers — sets up the per-tab record array and the
// serialization helper used by logpoint breakpoint conditions. Runs at
// document_start in the MAIN world on every page (and in every frame,
// per all_frames: true). The guards make this idempotent across
// re-injection and across the wrap userscript also defining
// __oddaSerialize (the first to run wins; the implementations are
// compatible).
//
// The breakpoint condition evaluates the agent's --expr in the paused
// frame's scope and calls window.__oddaLogpointPush(record). The
// condition has access to globalThis/window, so it can reach these
// helpers. Records are wiped on navigation (this userscript re-runs on
// every document_start and re-initializes the array). Per ADR-0004.

if (!window.__oddaSerialize) {
  window.__oddaSerialize = function(v, depth, seen) {
    if (v === null) return null;
    if (v === undefined) return undefined;
    var t = typeof v;
    if (t === 'function') {
      try {
        var src = String(v);
        if (src.length > 1000) src = src.substring(0, 1000) + '...';
        return {type: 'function', name: v.name || null, source: src};
      } catch (e) {
        return {type: 'function', name: v.name || null};
      }
    }
    if (t === 'string') {
      if (v.length > 10000) return {type: 'string', truncated: true, length: v.length, preview: v.substring(0, 10000)};
      return v;
    }
    if (t === 'number' || t === 'boolean' || t === 'bigint') return v;
    if (t === 'symbol') return {type: 'symbol', description: v.description || null};
    if (t !== 'object') return String(v);
    if (seen.has(v)) return {type: 'object', truncated: true, cycle: true};
    if (depth > 5) return {type: 'object', truncated: true, depth: true};
    var ns = new Set(seen);
    ns.add(v);
    if (Array.isArray(v)) {
      if (v.length > 100) return {type: 'array', truncated: true, length: v.length};
      var arr = [];
      for (var i = 0; i < v.length; i++) arr.push(window.__oddaSerialize(v[i], depth + 1, ns));
      return arr;
    }
    try { if (v instanceof Node) return {type: 'node', name: v.nodeName || null, tag: v.tagName || null}; } catch (e) {}
    var keys;
    try { keys = Object.keys(v); } catch (e) { return {type: 'object', truncated: true, error: String(e)}; }
    if (keys.length > 50) return {type: 'object', truncated: true, keys: keys.slice(0, 50)};
    var obj = {};
    for (var ki = 0; ki < keys.length; ki++) {
      var k = keys[ki];
      try {
        var d = Object.getOwnPropertyDescriptor(v, k);
        if (d && !('value' in d)) { obj[k] = {type: 'accessor'}; continue; }
        var cv = d ? d.value : v[k];
        obj[k] = window.__oddaSerialize(cv, depth + 1, ns);
      } catch (e) { obj[k] = {type: 'error', message: String(e)}; }
    }
    return obj;
  };
}
if (!window.__oddaLogpointPush) {
  window.__oddaLogpointPush = function(rec) {
    // Aggregate to the top frame when accessible (same-origin iframes)
    // so `odda logpoint dump` on the main tab sees records from all
    // same-origin frames. Cross-origin iframes keep their own array.
    try {
      var top = window.top;
      if (top !== window && top.__oddaLogpointPush) {
        top.__oddaLogpointPush(rec);
        return;
      }
    } catch (e) { /* cross-origin: fall through to local */ }
    if (!window.__oddaLogpoint) window.__oddaLogpoint = [];
    window.__oddaLogpoint.push(rec);
  };
}
if (!window.__oddaLogpoint) window.__oddaLogpoint = [];