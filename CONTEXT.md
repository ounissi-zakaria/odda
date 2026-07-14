# odda

## Language

**Test document**:
A Scrut Markdown file under `tests/e2e/scrut/` that exercises a slice of odda's CLI surface. Each document is independently runnable and gets its own working directory and odda server.
_Avoid_: test file, test script

**Test suite**:
The full collection of test documents in `tests/e2e/scrut/`, run together via `scripts/test-e2e.sh` inside the test container.
_Avoid_: test set, test collection

**Test container**:
A Docker image, built from `python:3.14-slim` + Google Chrome, that provides the complete environment for running the test suite. Contains `odda` installed on `$PATH`, `scrut`, Chrome, and all system tools the test docs need. Runs as a non-root user.
_Avoid_: test image, test box

**Per-doc server**:
An odda server instance booted by a single test document in its own `$PWD`, using a per-document socket path and data directory. Torn down at the document's end. No server state crosses document boundaries.
_Avoid_: shared server, global server

**Shared boot/teardown**:
The `_lib/boot.md` (prepended) and `_lib/teardown.md` (appended) files that DRY the odda server start and stop across all test documents. Each document still gets its own per-doc server instance; only the prose is shared.
_Avoid_: setup file, fixture file