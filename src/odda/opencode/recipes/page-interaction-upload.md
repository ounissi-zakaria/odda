# Recipe: interact with a page by snapshot and ref (forms, uploads, clicks)

Snapshot the page to discover element **refs** (`eN`), then pass a ref to `page click` / `page fill` / `page upload` to act on that element. This is the ref-driven alternative to hand-written CSS selectors via `odda eval`, and is more robust on minified SPAs where selectors are unstable but the a11y tree is stable. See [SKILL.md](../SKILL.md) for the command reference.

## 1. Open a browser and navigate

```
odda browser open
# returns {browser_id, tab_id, status}
odda navigate --url <url> --browser-id <B> --tab-id <T>
```

## 2. Snapshot to find refs

```
odda page snapshot --browser-id <B> --tab-id <T>
# returns the a11y tree as YAML-ish text with [ref=eN] tags
# grep the text for the element you want (a textbox, a button, a link)
```

Refs are valid until the element leaves the DOM (navigation, SPA swap). Re-snapshot after any content change; existing refs keep working without re-snapshotting.

## 3. Fill form fields by ref

```
odda page fill --ref <ref> --value "<value>" --browser-id <B> --tab-id <T>
```

Fill each input/textarea/select in turn, then snapshot again if you need the submit button's ref (it may have appeared after the page settled).

## 4. Click buttons / links by ref

```
odda page click --ref <ref> --browser-id <B> --tab-id <T>
```

Clicking may trigger navigation or an SPA swap — snapshot again to read the new state. Refs from the pre-click snapshot are stale after a navigation.

## 5. Handle file inputs (the aria-label gotcha)

Playwright omits a nameless `<input type="file">` from the a11y tree, so it won't appear in the snapshot. Give it an `aria-label` via `eval`, re-snapshot, then upload by the new ref:

```
odda eval --js "document.querySelector('input[type=file]').setAttribute('aria-label','upload')" --browser-id <B> --tab-id <T>
odda page snapshot --browser-id <B> --tab-id <T>      # now the input has a ref
odda page upload --ref <ref> --file <path> --browser-id <B> --tab-id <T>
```

After `upload` sets the file on the input, click the form's submit button by ref to POST it.

## Worked example: polyglot web shell upload

The PortSwigger "RCE via polyglot web shell upload" lab needs a PHP/JPG polyglot uploaded as an avatar. Snapshot the login page, fill username + password by ref, click "Log in". On the account page the file input has no accessible name, so `eval` an `aria-label` onto it, re-snapshot, then `page upload` the polyglot (`exiftool -Comment="<?php ... ?>" input.jpg -o polyglot.php`) by the new ref and click "Upload". Navigating to `/files/avatars/polyglot.php` executes the PHP; read the secret out of `document.body.innerText` via `eval`, then POST it to `/submitSolution` with a `fetch` from `eval` to clear the lab.