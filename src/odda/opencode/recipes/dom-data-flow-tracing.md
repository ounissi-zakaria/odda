# Recipe: DOM data-flow tracing (source to sink)

A worked example of the three dynamic-analysis concepts (Wrap + Coverage + Logpoint) composing with page interaction to trace untrusted data from where it enters the page (a `postMessage` handler) to where it's checked or sinks. The same pattern applies to any DOM data-flow question — hashchange listeners, URL-parameter consumers, `location.hash`-to-`innerHTML` flows — by pointing Wrap/Coverage/Logpoint at the relevant source or sink. See [SKILL.md](../SKILL.md) for the command surface and [DYNAMIC-ANALYSIS.md](../DYNAMIC-ANALYSIS.md) for the per-concept reference.

The workflow: Wrap confirms the API touch, Coverage finds the code path, Logpoint reads the locals at the interesting line. Each concept observes without modifying behavior (the page never pauses).

## 1. Wrap `addEventListener` to confirm a `message` handler is registered and capture the handler reference

```
odda wrap calls add --browser-id 1 --tab-id 1 --expr EventTarget.prototype.addEventListener --name ael
odda navigate --url http://target/ --browser-id 1 --tab-id 1   # re-navigate so the wrap runs
odda eval --js "String(window.__oddaWrapFixture(window, 'message', function onMsg() {}))" --browser-id 1 --tab-id 1
odda wrap dump --browser-id 1 --tab-id 1   # confirm 'message' registration, capture handler ref
```

## 2. Coverage to find which code path the handler runs when a message arrives

```
odda coverage start --browser-id 1 --tab-id 1
odda eval --js "window.postMessage({type: 'probe'}, '*')" --browser-id 1 --tab-id 1
odda coverage stop --browser-id 1 --tab-id 1   # find the script URL + block ranges that ran
```

## 3. Read the handler source to find the origin-check line

Read the handler source from the captured flow body (the script URL from coverage maps to a flow in `.odda/flows/`) to find the origin-check line.

## 4. Logpoint at that line to read the locals the check operates on

```
odda logpoint add --browser-id 1 --tab-id 1 --url <script-url> --line <n> --col <n> --expr "event.origin"
odda eval --js "window.postMessage({type: 'probe'}, 'https://evil/')" --browser-id 1 --tab-id 1
odda logpoint dump --browser-id 1 --tab-id 1   # read the captured origin value
```

## 5. Verdict

If the logpoint records an attacker-controllable origin, the handler does not validate origin and is vulnerable.

## With page interaction (user-triggered behavior)

The same recipe composes with `odda page` when the behavior is triggered by user input rather than a `postMessage` call. Snapshot to find the form/button, click or fill to trigger the behavior, then observe with Wrap/Coverage/Logpoint:

1. **Snapshot** to find the target:
   ```
   odda page snapshot --browser-id 1 --tab-id 1   # find the ref for the submit button
   ```
2. **Wrap** `EventTarget.prototype.addEventListener` (as above), then **click** the button to trigger the handler:
   ```
   odda page click --browser-id 1 --tab-id 1 --ref e2   # click the submit button by ref
   ```
3. **Coverage** to find which code path ran as a result of the click, **Logpoint** to read locals at the interesting line — same as above.

The page-interaction step replaces `odda eval --js "window.postMessage(...)"` when the trigger is a user action (form submit, button click, file upload) rather than a programmatic call.