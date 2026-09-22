"""odda - Browser automation and HTTP traffic capture CLI for AI agents."""

__version__ = "0.53.1"

#: Playwright ``page.goto`` lifecycle events accepted by navigate, in firing
#: order. The single source of truth for the MCP server's ``navigate``
#: param validation and error message; the browser layer passes the value
#: through to Playwright. Lives here so importing it stays lightweight —
#: no pull-in of the patchright-backed ``odda.browser`` module.
NAVIGATE_WAIT_UNTIL_EVENTS: tuple[str, ...] = (
    "commit",
    "domcontentloaded",
    "load",
    "networkidle",
)
