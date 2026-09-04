# Interview prompt: agent usage of odda

Paste this prompt to any agent that has used `odda`. It is structured so responses are concrete and triageable rather than vague impressions.

---

You are being interviewed about your experience using the `odda` MCP server (tools like `browser_open`, `navigate`, `page_snapshot`, `request_send`) for browser automation, HTTP traffic capture, dynamic analysis, and raw request crafting. The goal is to find rough edges so the tool can be improved. Be specific and honest — "it worked fine" is not useful; concrete tool calls, exact errors, and the results you got back are.

**Before answering:** If you have a transcript or memory of a specific session where you used odda, ground your answers in that session (quote the actual commands you ran and the responses you got). If not, answer from your general experience. Say which case applies at the top.

## 1. Task & outcome
- What were you trying to accomplish with odda? (drive a browser, capture traffic, observe JS execution, craft a raw request, or a combination)
- Did you succeed? If you gave up or switched to a different approach, say so and why.

## 2. What went wrong (bugs / errors / crashes)
First, capture the environment so reports are reproducible — paste the output of the `version` tool, and note the OS and Chrome version if you know them.

For each problem, give: the exact tool call(s) you made (arguments, in a fenced code block), the error or result you got back, what you expected, and what you did to work around it. Include tool errors, hangs, wrong results, and anything that felt like a bug even if you're unsure.

## 3. What was unexpected (worked, but surprised you)
Things that succeeded but behaved differently from your mental model — defaults, naming, output shape, lifecycle, ordering. These are the UX/confusion signals. For each: what you expected vs. what happened.

## 4. Docs & resources
- Was there anything you couldn't find in the docs and had to discover by trial and error?
- Did you read the `odda://docs/` resources (`flows`, `request-crafting`, `dynamic-analysis`, `userscripts`, `proxy-scripts`, `recipes`)? If yes, were they accurate? If no — did you know they existed?
- Anything documented that was wrong, stale, or contradicted by actual behavior?
- Anything you only understood *after* failing once? (the failure that taught you is the signal we want)

## 5. Specific surfaces — only answer for the ones you touched
- **Targeting model & IDs:** Did the `browser_id` / `tab_id` model make sense? Any stale-ID or wrong-tab confusion? Multiple browsers in one session?
- **Snapshot + refs:** Did the snapshot→ref→action workflow work? Stale refs, elements missing from the a11y tree (e.g. file inputs), cross-iframe refs?
- **`eval` / `wait_for`:** Serialization surprises (double-encode, `Response` → `{}`, Promises), expression-vs-return confusion, timeout behavior?
- **Screenshot vs snapshot:** Did you pick the right one, or reach for the wrong tool first?
- **Userscripts:** Install/reload lifecycle, the dialog interceptor (`__oddaDialogs` / `__oddaDialogResponses`), cross-frame/cross-nav limits?
- **Dynamic analysis (wrap / logpoint / coverage):** Did you pick the right one for what you knew? The **records-wipe-on-navigation** rule — did it bite you? Logpoint `col` requirement? Coverage delta vs cumulative?
- **Traffic capture & flows:** Reading `.odda/flows/<id>/`, response-body decoding, `flows.jsonl` schema?
- **Raw request crafting:** `request_clone`/`request_new`/`request_send`, CRLF gotchas, `meta.json` host vs `Host` header, HTTP/2 framing, `fix_content_length`?
- **Session lifecycle:** Did the MCP server start cleanly from your harness config? Browser/proxy teardown on session end? Anything in the server's stderr you needed but couldn't see?

## 6. Missing features & workarounds
- Anything you wanted to do that odda can't do at all?
- Anything you had to shell out to `eval` / raw JS / external tools to accomplish that odda should arguably handle natively?
- Any operation that was noticeably slow (single tool call > 5s, or repeated calls adding up)? Note the tool and rough wall-clock time.

## 7. Severity & priority
- Rank your top 3 issues by how much they blocked you (not by frequency). Use this scale:
  - **blocker** — couldn't complete the task; gave up or switched tools
  - **major** — completed the task but lost significant time or confidence
  - **minor** — annoying; would fix if free
- For each, one line on the fix you'd want (behavior change, doc fix, new flag, new command).

Be terse. Bullet points over prose. Skip any section that doesn't apply to you.

## 8. Output
Compile your answers above into a single Markdown file at `odda-feedback-<YYYY-MM-DD>-<short-slug>.md` in your current working directory (use today's date and a 2-4 word slug describing the session, e.g. `odda-feedback-2026-07-24-csp-bypass-attempt.md`). Structure the file with one `##` heading per section above that you answered, and only include sections you have substantive answers for — omit empty ones. Keep the bullet-point terseness; do not pad with prose. If you ran specific commands, include them in fenced code blocks so they're greppable. After writing, print the file path so it can be collected.