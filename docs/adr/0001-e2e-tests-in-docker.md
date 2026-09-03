# Run e2e tests inside a Docker container
Status: superseded as the documented runner — the suite now runs on the host
(the MCP migration removed the shared-server/socket model whose isolation this
defended), and `scripts/test-e2e.sh` is deleted (its prefix-selection and
parallelism are native pytest now: `pytest <file>` selection, `-n 4` default in
`pytest.ini` addopts). `tests/e2e/Dockerfile` is kept as the seed image for a
future CI runner.


The e2e suite runs inside a Docker image built from `python:3.14-slim` + Google Chrome,
invoked via `scripts/test-e2e.sh`. This isolates the test odda from the developer's
installed/running odda, eliminates the `_lib/setup.md` `pkill` sweeps, and gives contributors
a self-contained test environment without host Chrome/Python requirements. The trade-off
is a Dockerfile + image build instead of direct host execution.

Per-doc odda servers are preserved (each test document boots its own server in its own
`$PWD`); only the boot/teardown prose is DRY'd into `_lib/boot.md` (prepended) and
`_lib/teardown.md` (appended). A shared single server across documents was rejected because
the test docs assume a fresh server (hard-coded browser/tab IDs, absence-of-flows
assertions) and sharing makes the suite order-dependent.