---
name: scrut
description: Write and run Scrut tests for a CLI — Markdown-based, executable documentation. Use when the user wants to add a CLI integration test, document a CLI's behavior, write a `.md` or `.t` test for `scrut test`, scaffold a tests directory with `scrut create`, debug a failing scrut test, or update stale output expectations with `scrut update`.
---

# Scrut — CLI testing in Markdown

Scrut is a Cram-style CLI testing framework where tests live inside Markdown (or `.t` Cram) files as `scrut`-fenced code blocks. The same document is the spec, the manual, and the executable test. The CLI under test must already exist (or be on `$PATH`) — Scrut orchestrates it, it does not build it.

Source of truth for the tool: <https://facebookincubator.github.io/scrut/>. When this skill and the upstream docs disagree, the docs win.

## Mental model

- **Test document** — a `.md` (preferred) or `.t` (Cram, legacy) file. Contains zero or more test cases plus freeform prose.
- **Test case** — one fenced ```` ```scrut ```` block. Inside: a `shell expression` (what to run) and any number of `output expectations` (what should come back). Optionally followed by a `[exit-code]` like `[1]`.
- **Shell expression** — the command(s) to execute. The first line starts with `$ `, continuation lines with `> `. Effectively a `bash` script.
- **Output expectation** — a line under the command predicting one line of STDOUT. Default is *equal*; you can suffix `(glob)`, `(regex)`, `(escaped)`, `(no-eol)` and add `?`/`*`/`+` quantifiers.

Everything else — env vars, working dir, timeouts, streaming, document-wide setup — follows from these primitives.

## Workflow

### 1. Pick the CLI to test and the first thing to assert

Start with a smoke test: does the binary run at all? `mycli --version > /dev/null` or `mycli --help > /dev/null` is the right opener. Expand from there into functional tests of subcommands, then integration tests against fixtures.

### 2. Lay out the tests directory

Recommended (matches the upstream tutorial):

```
mycli/
├── src/
└── tests/
    ├── smoke.md            # binary is executable, --help / --version
    ├── fixtures/           # stable inputs (json, txt, ...) used by tests
    ├── setup.md            # shared setup doc, prepended to others
    └── <feature>.md        # one file per behavior / use case
```

One file per use case (the "coherent test suite" pattern) beats one mega-file — failures point straight at the broken feature.

### 3. Make the CLI under test reachable

Scrut runs whatever you put in the fenced block, in `bash`. The CLI must be resolvable from the runner's `$PATH`, or you must write the absolute path in every test case. Three common patterns:

- **On `$PATH`.** Easiest. `cargo install`, `pip install -e .`, or copy the binary somewhere on `$PATH`. Then `mycli ...` works in every case.
- **Path-via-env-var.** Set the env var in the shell that runs scrut, reference it inside every test case:

  ```bash
  MYCLI=$(pwd)/target/debug/mycli scrut test tests/
  ```

  ````markdown
  ```scrut
  $ "$MYCLI" --version
  0.1.0
  ```
  ````

  Keeps the test docs clean and lets CI swap the binary path per job.
- **Path-via-setup-case.** First test case `cd`s to the binary's directory or exports `PATH="$TESTDIR/../target/debug:$PATH"`. Brittle — prefer the env-var pattern.

Whatever you choose, the binary-not-found failure mode is silent and confusing: an empty `STDOUT`, a `: command not found` on `STDERR`, and an exit-code failure. If a fresh test run shows that pattern across many cases, the binary isn't on `$PATH` — fix the runner, not the tests.

### 4. Scaffold a test

**Use `scrut create` to bootstrap** — never hand-author the first cut:

```bash
scrut create --output tests/smoke.md -- mycli --version
echo "mycli --version" | scrut create - > tests/smoke.md
```

This writes a valid test case with the current output captured, ready to run.

**By hand**, the smallest test is:

````markdown
# Smoke: binary runs

```scrut
$ mycli --version > /dev/null
```
````

The trailing output lines are optional. With none, only the exit code is checked (defaults to `0`).

### 5. Add output expectations

For each command, predict one line of STDOUT per line you expect back. Use the simplest matcher that holds:

| You want to assert              | Write                       | Notes                                            |
| ------------------------------- | --------------------------- | ------------------------------------------------ |
| Exact line                      | `Hello`                     | Default; trailing newline required               |
| No trailing newline             | `Hello (no-eol)`            | Rare; only as the last line                      |
| Whitespace / version noise      | `Hello* (glob)`             | `?` = one char, `*` = any                        |
| Shape (e.g. semver, dates)      | `v1\.\d+\.\d+ (regex)`      | Always anchored — use `.*` to widen              |
| Tabs / ANSI / non-printable     | `col1\tcol2 (escaped)`      | `\x1b`, `\t`, `\n` etc.                          |
| Zero or more lines of a pattern | `*,* (glob+)`               | Quantifiers: `?` `*` `+`                          |

The line **before** the fence becomes the test case's *title* (used in failure output).

Full BNF and examples: <https://facebookincubator.github.io/scrut/docs/reference/fundamentals/output-expectations/>.

### 6. Stabilize the test

Three rules, in order of preference:

1. **Pin the input with a fixture.** Replace `curl https://api.example.com/...` with `cat "$TESTDIR"/fixtures/response.json | mycli ...`. Fixtures live next to the test and remove network / clock / external-service flakiness.
2. **Generalize the output with a matcher.** Swap exact `equal` expectations for `(glob)` / `(regex)` / `(escaped)` only where the *exact* value isn't the thing under test.
3. **Configure the CLI to be deterministic.** `mycli --no-color --no-ansi --format=json` etc., usually set up once via a `setup.md` document or an `alias` in the first test case.

Reach for the first option first; reach for the last only when the CLI has no flag for it.

### 7. Use the right env vars and directories

Scrut injects these for every test case (don't redefine them):

- `$TESTDIR` — absolute path of the directory holding the current test document. Use this to reference fixtures: `cat "$TESTDIR"/fixtures/x.json`.
- `$TMPDIR` — fresh shared temp dir for the run (cleaned up after). Use for any file the test creates and consumes.
- `$PWD` — current working directory; isolated per test document, cleaned up after.
- `$SCRUT_TEST` — `path/to/doc.md:line`. Use this inside the CLI itself to detect "am I under test".
- `$TESTSHELL` — the shell Scrut is using (`/bin/bash` by default).

Other env is normalized to a stable baseline: `LANG=C`, `LC_ALL=C`, `TZ=GMT`, etc. — no surprise locale failures.

### 8. Share setup and teardown across tests and documents

Three escalation levels for **setup**, **prefer the first**:

1. **In-document setup case** — a first test case that defines an `alias` or exports a var, e.g. `alias mycli_run='mycli --no-color --json'`. Subsequent cases inherit. Use it when only this document needs the setup.
2. **Shared `setup.md` + `prepend`** — put the setup in its own file and prepend it to every test document via front matter:

   ```markdown
   ---
   prepend:
     - setup.md
   ---
   ```

   Or globally with `scrut test -P setup.md tests/`. Per-document config (incl. `defaults`) in prepended docs is ignored — keep it pure setup.

3. **`source "$TESTDIR"/setup.sh`** — when setup is more than an alias, keep it in a real `.sh` file and `source` it.

**Teardown** is its own problem and `prepend` is the wrong tool for it (it runs *before* the document's cases, not after). Two workable patterns:

- **Last test case in each doc** does the cleanup, e.g. `pkill -f "mycli server --socket $PWD/sock" || true`. The last-case position means it always runs, regardless of whether earlier cases failed.
- **`append:`** (a `setup.md` analogue for teardown) — same `prepend:` syntax but it runs after the document's cases. Pairs naturally with `prepend:` for symmetric setup/teardown.

**Cross-doc cleanup is its own thing.** If a test doc starts a long-lived process (server, helper HTTP listener) and the doc fails partway through, the process leaks into the next run. The fix is a prepended `setup.md` whose first cases are `pkill` calls, e.g. `pkill -f "mycli server --socket /tmp/run-.*" 2>/dev/null || true`. They run before every doc, sweep leftovers, and themselves always succeed (`|| true` and a trailing `pgrep ... || echo "clean"`). Pair with the last-case cleanup in each doc and you get hermetic runs.

**Shell state persists between test cases.** `export FOO=bar`, `alias x=y`, `FOO=bar`, and `id=$(mycli new)` in one case are all visible in the next. Scrut writes the shell state to a `state` file after every case and sources it before the next one — see [Execution Model](https://facebookincubator.github.io/scrut/docs/reference/behavior/execution-model/). Two real exceptions: (a) `detached` test cases do not propagate their state, and (b) each case runs in its own `bash` process, so anything *only* in the current process (open file descriptors, backgrounded jobs, `trap` handlers) does not survive. If you need to pass a file path or value to a later case, you can — but use the variable directly, don't bother writing to `$PWD` and `cat`-ing it back.

### 9. Cover the awkward cases

- **Non-zero exit codes** — append `[<n>]` to the test: `mycli --bad-flag\n[2]`. Exit code is checked before output.
- **Skip an entire document** — first case ends in `exit 80` (configurable via `skip_document_code`). Useful for OS- or arch-gated tests.
- **Test STDERR** — set `output_stream: stderr` or `combined` on the test case. Default is STDOUT only.
- **Color / ANSI noise** — add `strip_ansi_escaping: true` to the test case, or pipe through `sed` in the command.
- **Line-ending sensitivity** — `keep_crlf: true` if your CLI is Windows-shaped.
- **Long-running case** — `timeout: 30s` per case, or `total_timeout: 5m` in the doc's front matter.
- **Detached server** — `{detached: true}` on the start case, then `{wait: {timeout: 10s, path: "sock"}}` on the consumer case. `detached_kill_signal: term` (default) cleans up.
- **Per-case env override** — `{environment: {FOO: "bar"}}` sets vars for that case only.
- **Output that *contains* a kind suffix** — write the kind explicitly so Scrut doesn't get confused: `Hello (equal) (equal)` matches a literal line of `Hello (equal)`.

### 10. Run and iterate

```bash
scrut test tests/                        # whole suite, silent unless failing
scrut test --verbose tests/smoke.md      # verbose: every case reported (no short flag)
scrut test --absolute-line-numbers ...   # line nums from doc start, not within case
scrut test -r diff failing.md            # unified diff renderer (pipeable to `patch`)
```

The default renderer is human-readable. Exit codes: `0` pass, `1` scrut error, `50` validation failure (tests ran but assertions failed).

When output has drifted but the *behavior* is still right, regenerate expectations:

```bash
scrut update tests/                       # interactive, writes <file>.new
scrut update -r -y tests/                 # replace in place, no prompt
```

Limits of `update`: only `equal` / `escaped` are updated; `glob` / `regex` get rewritten as `equal`; quantifiers are flattened to repeated lines; prepended/appended docs are not updated (update them individually).

### 11. Wire it into CI

The pattern from the docs, ported:

```yaml
# .github/workflows/scrut.yml
name: scrut
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: facebook/install-dotslash@latest
      - run: |
          curl -LsSf https://github.com/facebookincubator/scrut/releases/latest/download/scrut > scrut
          chmod +x scrut
      - run: ./scrut test tests/
```

For full reproducibility, pin the version (`/download/vX.Y.Z/scrut`) or use the Docker image `ghcr.io/facebookexternal/scrut:<VERSION>` with `./tests` mounted at `/app`. In Docker, prepend a doc that exports `PATH="/app/dist:$PATH"` so the test files don't hardcode binary locations.

## Common pitfalls

- **Multiline shell commands must be prefixed `>`, not `$`.** Only the first line is `$`; everything else in the same fenced block is `>`. The expression runs as one bash script.
- **Output expectations are matched in order, line by line.** If your command prints an unpredictable first line, you have to `(glob)` it — there's no skip-line primitive.
- **The shell expression's exit code is the *last* command's exit code.** `false ; true` exits `0`. Use `&&` or `set -e` (with caution — see Shell Expressions docs) when you care.
- **Markdown Scrut uses one `bash` process per test case**, with a shared state file. Cram uses one process per document. Don't mix them in the same file.
- **Shell *variables* (including command-substitution results) do survive between test cases** — Scrut persists them via a state file. So `id=$(mycli new)` in one case is visible in the next. Don't bother writing to `$PWD` and re-`cat`-ing unless you also want the value visible outside the test runner.
- **STDOUT vs STDERR** — Scrut defaults to STDOUT only. `curl`'s progress bar is on STDERR and will be ignored, which is usually what you want.
- **Quantifiers on `equal` mean *repeated identical lines***. `Hello (equal+)` requires one or more lines of exactly `Hello`. Use `(glob+)` for "one or more lines *like* Hello".
- **Regex is RE2, not PCRE.** No look-around / backreferences. All matches are anchored (`^...$`) — use `.*` to widen.
- **Front matter must be valid YAML between `---` fences at the very top of the file.** A typo there fails the whole document.
- **Don't use command-line flags to set things the doc could set itself.** It breaks the "the document is the spec" property — replicating a run requires knowing the exact `scrut test` invocation.
- **`detached_kill_signal` kills the test-case bash, not the orphaned child.** If your shell expression backgrounded something with `&` or `nohup &`, Scrut's kill signal hits the bash that already exited, not the child. Track the child's PID yourself and `kill` it in a teardown case, or have the child write its PID to a file in `$PWD` and `kill "$(cat "$PWD/pid")"` later.
- **Cross-doc process leaks.** A test doc that starts a long-lived process (server, helper listener) and fails partway through leaves it running for the next run. A prepended cleanup doc (cases that `pkill` by pattern and exit 0) is the standard fix; pair it with a per-doc teardown case.

## Cheat sheet — first 60 seconds of a new test

```bash
# 1. Bootstrap
scrut create --output tests/<feature>.md -- mycli <subcmd>

# 2. Tweak expectations in your editor

# 3. Run it
scrut test --verbose tests/<feature>.md

# 4. When outputs drift in a good way
scrut update -r -y tests/<feature>.md
```
