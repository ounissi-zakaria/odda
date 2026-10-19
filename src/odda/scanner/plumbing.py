"""Shared scan plumbing: the parser guard and the GIL-cadenced chunks.

The binary-free leaf of the scanner package: the rule-author surface
(``odda.scanner.model``) and the literal gates import only this
module, so importing the contract never pulls the Vectorscan
extension — ``fastscan`` keeps the engine, engine-internal only.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

try:  # The scanner's one private-parser import guard (private API, but
    # the only parser shipped): the literal gates, the engine's
    # expansion screen, and the corpus shape screen all parse patterns
    # through this pair. A missing module just turns every consumer
    # off — gates pass, patterns stay on the stdlib path.
    from re import _constants as sre_constants
    from re._parser import parse as sre_parse
except ImportError:  # pragma: no cover
    sre_constants = None  # type: ignore[assignment]
    sre_parse = None  # type: ignore[assignment]

_T = TypeVar("_T")

# Slice length of the shared GIL cadence (see ``chunked``): one 10 MB
# text pass is a single uninterruptible C call that stalls the event
# loop serving the drain for its whole duration; the slices drop the
# GIL between chunks, so the loop keeps its cadence whatever the body
# size (ticket 12's numbers contract).
GIL_CHUNK = 1 << 18


def chunked(
    items: Sequence[_T], chunk: int = GIL_CHUNK, *, step: int | None = None
) -> Iterator[_T]:
    """Yield ``items`` in bounded slices, offering the GIL between them.

    The GIL-yield contract every chunked scan pass shares (folds,
    containment windows, decodes, reversals): each slice keeps the
    caller's work one short C call, and this generator sleeps zero
    after every yield, so the event loop serving the drain regains the
    GIL between slices and keeps its cadence whatever the body size.
    A caller returning early abandons the generator — no trailing
    sleep, no cleanup to run.

    Args:
        items: The sequence to slice (a ``str`` slices into ``str``).
        chunk: Slice length; the shared GIL cadence by default.
        step: Distance between slice starts (``chunk`` by default).
            Below ``chunk`` the slices overlap — window scans pass
            ``chunk - len(needle) + 1`` so an occurrence straddling a
            boundary is never missed.
    """
    stride = chunk if step is None else step
    for start in range(0, len(items), stride):
        yield items[start : start + chunk]
        time.sleep(0)  # offer the loop thread the GIL between slices


def read_chunked(path: Path, chunk: int = GIL_CHUNK) -> Iterator[bytes]:
    """Read a stored file in bounded chunks, offering the GIL between.

    The same GIL-yield contract as :func:`chunked`, over a file: each
    read is one bounded C call and the generator sleeps zero after
    each yield. The handle closes when the generator is exhausted or
    abandoned (a generator's ``with`` closes on collection).
    """
    with path.open("rb") as handle:
        while data := handle.read(chunk):
            yield data
            time.sleep(0)  # offer the loop thread the GIL between reads
