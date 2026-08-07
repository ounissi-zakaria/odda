"""Editable raw HTTP request storage and sending.

Stores editable request copies under ``<DATA_DIR>/requests/<name>/`` (a
``request`` file with raw HTTP/1.x-shaped bytes + a ``meta.json`` sidecar
holding scheme/host/port). ``send`` reads those files, opens a socket (TLS
for https, ALPN h2 for HTTP/2 request lines), writes the exact bytes on the
wire, reads the response, decodes it, and writes a flow record via
:mod:`odda.flowstore` so sent requests appear in the same ``flows.jsonl``
index as captured ones.
"""

from __future__ import annotations

from odda.request.api import clone, new, send, send_pipeline, send_repeat

__all__ = ["clone", "new", "send", "send_pipeline", "send_repeat"]
