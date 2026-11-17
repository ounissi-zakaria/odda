"""Plain (ws://) hypercorn boot of the dyn_asgi app for capture tests.

The shared dyn_server is TLS-only (wss via CONNECT tunnel); this script
binds a plaintext hypercorn on ``127.0.0.1:<port>`` serving the same
ASGI app, so the ``ws://`` upgrade path — an absolute-URI GET through
the proxy, no CONNECT tunnel — gets its own leg. The sibling import
works because ``python3 ws_echo_plain.py`` puts this directory on
``sys.path``.

Run as: python3 ws_echo_plain.py <port>
"""

from __future__ import annotations

import asyncio
import sys

from dyn_asgi import app
from hypercorn.asyncio import serve
from hypercorn.config import Config


def main() -> None:
    """Serve the dyn_asgi app over plaintext ws/http on the given port."""
    port = int(sys.argv[1])
    config = Config()
    config.bind = [f"127.0.0.1:{port}"]
    asyncio.run(serve(app, config))


if __name__ == "__main__":
    main()
