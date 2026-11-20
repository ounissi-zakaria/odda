__version__ = "0.63.0"

NAVIGATE_WAIT_UNTIL_EVENTS: tuple[str, ...] = (
    "commit",
    "domcontentloaded",
    "load",
    "networkidle",
)
