(() => {
  if (window.__oddaDialogInterceptorInstalled) {
    return;
  }
  window.__oddaDialogInterceptorInstalled = true;

  const ENTRY_LIMIT = 1000;
  window.__oddaDialogs = [];

  function record(type, message, defaultValue) {
    const entry = {
      type,
      url: location.href,
      timestamp: Date.now(),
      stack: new Error().stack || null,
    };
    if (message !== undefined) {
      entry.message = message == null ? "" : String(message);
    }
    if (defaultValue !== undefined) {
      entry.defaultValue = defaultValue == null ? "" : String(defaultValue);
    }
    window.__oddaDialogs.push(entry);
    if (window.__oddaDialogs.length > ENTRY_LIMIT) {
      window.__oddaDialogs.shift();
    }
    return entry;
  }

  // print opens the system print dialog and blocks the page, so we
  // record the call but do not forward it to the native handler.
  window.print = function print() {
    record("print");
  };

  const nativeAlert = window.alert;
  window.alert = function alert(message) {
    record("alert", message);
    if (typeof nativeAlert === "function") {
      nativeAlert.call(this, message);
    }
  };

  // confirm/prompt proceed by default (ADR-0011): confirm returns true,
  // prompt returns "odda", so the page proceeds instead of being silently
  // denied by headless Chrome's native handlers. The agent overrides
  // per-type via window.__oddaDialogResponses (agent-owned; the interceptor
  // reads it only and never resets it, so a userscript setting it at
  // document_start is visible to on-load prompts). Registered values pass
  // through verbatim — no type coercion.
  function registeredResponse(type, fallback) {
    const map = window.__oddaDialogResponses;
    if (map && Object.prototype.hasOwnProperty.call(map, type)) {
      return map[type];
    }
    return fallback;
  }

  window.confirm = function confirm(message) {
    const entry = record("confirm", message);
    entry.result = registeredResponse("confirm", true);
    return entry.result;
  };

  window.prompt = function prompt(message, defaultValue) {
    const entry = record("prompt", message, defaultValue);
    entry.result = registeredResponse("prompt", "odda");
    return entry.result;
  };
})();
