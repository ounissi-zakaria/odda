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

  const nativeConfirm = window.confirm;
  window.confirm = function confirm(message) {
    const entry = record("confirm", message);
    if (typeof nativeConfirm === "function") {
      entry.result = nativeConfirm.call(this, message);
      return entry.result;
    }
    entry.result = false;
    return false;
  };

  const nativePrompt = window.prompt;
  window.prompt = function prompt(message, defaultValue) {
    const entry = record("prompt", message, defaultValue);
    if (typeof nativePrompt === "function") {
      entry.result = nativePrompt.call(this, message, defaultValue);
      return entry.result;
    }
    entry.result = null;
    return null;
  };
})();
