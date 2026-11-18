__version__ = "0.61.1"

NAVIGATE_WAIT_UNTIL_EVENTS: tuple[str, ...] = (
    "commit",
    "domcontentloaded",
    "load",
    "networkidle",
)
