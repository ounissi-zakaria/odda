from __future__ import annotations

import time
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

try:
    from re import _constants as sre_constants
    from re._parser import parse as sre_parse
except ImportError:
    sre_constants = None  # type: ignore[assignment]
    sre_parse = None  # type: ignore[assignment]

_T = TypeVar("_T")

GIL_CHUNK = 1 << 18


def chunked(
    items: Sequence[_T], chunk: int = GIL_CHUNK, *, step: int | None = None
) -> Iterator[_T]:
    stride = chunk if step is None else step
    for start in range(0, len(items), stride):
        yield items[start : start + chunk]
        time.sleep(0)


def read_chunked(path: Path, chunk: int = GIL_CHUNK) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while data := handle.read(chunk):
            yield data
            time.sleep(0)
