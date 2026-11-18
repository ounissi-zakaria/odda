from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from odda import flowstore
from odda.request.types import META_FILENAME, REQUESTS_DIR_NAME, EditableMeta


def requests_dir() -> Path:
    d = flowstore.DATA_DIR / REQUESTS_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_meta(req_dir: Path) -> EditableMeta:
    data = json.loads((req_dir / META_FILENAME).read_text(encoding="utf-8"))
    lt = data.get("line_terminator")
    lt = b"\r\n" if lt is None else bytes(lt)
    return EditableMeta(
        scheme=data.get("scheme", "https"),
        host=data.get("host", ""),
        port=int(data.get("port", 443)),
        line_terminator=lt,
    )


def write_meta(req_dir: Path, meta: EditableMeta) -> None:
    payload: dict[str, Any] = {
        "scheme": meta.scheme,
        "host": meta.host,
        "port": meta.port,
    }
    if meta.line_terminator != b"\r\n":
        payload["line_terminator"] = list(meta.line_terminator)
    (req_dir / META_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def resolve_name_dir(name: str, *, force: bool) -> Path:
    req_dir = requests_dir() / name
    if req_dir.exists():
        if not force:
            msg = f"Request '{name}' already exists. Use --force to overwrite."
            raise ValueError(msg)
        shutil.rmtree(req_dir)
    req_dir.mkdir(parents=True, exist_ok=True)
    return req_dir
