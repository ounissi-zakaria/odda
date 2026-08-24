"""odda - Browser automation and HTTP traffic capture CLI for AI agents."""

__version__ = "0.40.10"

#: Playwright ``page.goto`` lifecycle events accepted by navigate, in firing
#: order. The single source of truth shared across the browser module (used by
#: the server for validation), the JSON-RPC handler, and the CLI (used to build
#: the ``--wait-until`` help text). Lives here so the CLI can import it without
#: pulling in the patchright-backed ``odda.browser`` module.
NAVIGATE_WAIT_UNTIL_EVENTS: tuple[str, ...] = (
    "commit",
    "domcontentloaded",
    "load",
    "networkidle",
)
