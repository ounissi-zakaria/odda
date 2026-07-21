## Browser fixture helpers

Defines `open_browser_fixture` and `navigate_fixture`. Each test
document that needs a live headless browser prepends this file (after
`_lib/fixture-server.md` if a fixture server is also used) and calls
`open_browser_fixture` with the path to navigate to and an optional
`wait-for` marker.

The fixture server port is read from `$PWD/fixture_port` (written by
`start_fixture_server`). Output is suppressed on success; stderr flows
through on failure so the cause of a setup failure is visible.

```scrut
$ open_browser_fixture() {
>   url_path="$1"; marker="${2:-}"
>   odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" browser open --headless > /dev/null
>   if [ -n "$url_path" ]; then
>     port=$(cat "$PWD/fixture_port")
>     odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>       navigate "http://127.0.0.1:$port$url_path" \
>       --browser-id 1 --tab-id 1 > /dev/null
>   fi
>   if [ -n "$marker" ]; then
>     odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>       wait-for "$marker" --browser-id 1 --tab-id 1 --timeout 10 > /dev/null
>   fi
> }
```

Navigate an already-open browser fixture to a new path, optionally
waiting for a marker. Reuses browser-id 1 / tab-id 1 set up by
`open_browser_fixture`. After the optional marker wait, a brief
settle delay lets Playwright's post-load internal setup (listener
registration, hit-target interceptors) complete so wraps like `ael`
on `addEventListener` don't catch Playwright's own calls.

```scrut
$ navigate_fixture() {
>   url_path="$1"; marker="${2:-}"
>   port=$(cat "$PWD/fixture_port")
>   odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>     navigate "http://127.0.0.1:$port$url_path" \
>     --browser-id 1 --tab-id 1 > /dev/null
>   if [ -n "$marker" ]; then
>     odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>       wait-for "$marker" --browser-id 1 --tab-id 1 --timeout 10 > /dev/null
>   fi
>   sleep 0.5
> }
```