# Three-level session configuration

Session settings — `max_browsers`, `stale_browser_timeout`, `vnc_bind` — come from TOML files (global under the odda config home, project in the data dir), overridden by `odda mcp` CLI flags, read once at session boot, and an unknown key or invalid value fails the boot with the file and key named. Files beat a new environment-variable surface because the operator already has a machine-global home (`~/.config/odda`, redirectable via `ODDA_CONFIG_HOME`, which the test suite already sandboxes) and a per-project data dir (`.odda/`), and flags cover the one-off spawn. Strict unknown-key rejection is deliberate: a typo'd key that silently no-ops looks like a working default and is the hardest misconfiguration to notice.

Considered options:

- Keeping the `ODDA_VNC_BIND` env var: removed outright (not deprecated) — the `--vnc-bind` flag was its only producer, and one mechanism should not have two spellings.
- Defining staleness as any session activity or page-side network activity: rejected — only a tool call that targets the browser counts, because a busy page is not an attentive agent; `browser_list` and unfiltered `tabs_list` probe without touching, so read-only sweeps never reset the clock. A stale close rides the normal close path, so the record stays reopenable.
