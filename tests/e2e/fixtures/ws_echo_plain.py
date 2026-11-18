from __future__ import annotations

import asyncio
import sys

from dyn_asgi import app
from hypercorn.asyncio import serve
from hypercorn.config import Config


def main() -> None:
    port = int(sys.argv[1])
    config = Config()
    config.bind = [f"127.0.0.1:{port}"]
    asyncio.run(serve(app, config))


if __name__ == "__main__":
    main()
