from __future__ import annotations

import asyncio
import difflib
import logging
import os
import re
import secrets
import shutil
import string
import subprocess
import tempfile
import time
import weakref
from contextlib import asynccontextmanager, suppress
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

from patchright.async_api import (
    BrowserContext,
    CDPSession,
    Dialog,
    Page,
    Playwright,
    async_playwright,
)
from PIL import Image, ImageDraw, ImageFont

from odda import (
    coverage as coverage_mod,
    logpoint as logpoint_mod,
    userscript as userscript_mod,
    wrap as wrap_mod,
)
from odda.chrome_args import build_chrome_args
from odda.config import OddaConfig
from odda.paths import base_profile_dir
from odda.proxy import AUTH_CANARY_URL, PROXY_AUTH_PASSWORD
from odda.vdisplay import (
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    VirtualDisplay,
    start_virtual_display,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

logger = logging.getLogger(__name__)

_FIND_CONTEXT_LINES = 3

# See ADR-0028.
_REF_BOX_RE = re.compile(
    r"\[ref=((?:f\d+)?e\d+)\]\s+\[box=(-?\d+),(-?\d+),(-?\d+),(-?\d+)\]"
)
_ANNOTATION_COLOR = (255, 0, 255)
_ANNOTATION_TEXT_COLOR = (0, 0, 0)
_ANNOTATION_CHIP_OUTLINE = (255, 255, 255)
_ANNOTATION_STROKE_WIDTH = 2
_ANNOTATION_FONT_SIZE = 13


def _draw_annotations(
    img: Image.Image, snapshot_text: str, dsf: float, target: Path
) -> str | None:
    # See ADR-0028.
    draw = ImageDraw.Draw(img)
    stroke_w = max(1, round(_ANNOTATION_STROKE_WIDTH * dsf))
    font = ImageFont.load_default(size=max(8, round(_ANNOTATION_FONT_SIZE * dsf)))
    rows: list[str] = []
    n = 0
    for line in snapshot_text.splitlines():
        m = _REF_BOX_RE.search(line)
        if m is None:
            continue
        ref = m.group(1)
        x, y, w, h = (int(m.group(i)) for i in range(2, 6))
        if w <= 0 or h <= 0:
            continue
        box = [
            round(x * dsf),
            round(y * dsf),
            round((x + w) * dsf),
            round((y + h) * dsf),
        ]
        draw.rectangle(box, outline=_ANNOTATION_COLOR, width=stroke_w)
        n += 1
        label = str(n)
        rows.append(f"{n},{ref},{x},{y},{w},{h}")
        tb = draw.textbbox((0, 0), label, font=font)
        pad = max(1, round(2 * dsf))
        chip_w = tb[2] - tb[0] + 2 * pad
        chip_h = tb[3] - tb[1] + 2 * pad
        cx = min(max(box[0], 0), img.width - chip_w)
        cy = min(max(box[1] - chip_h - pad, 0), img.height - chip_h)
        draw.rectangle(
            [cx, cy, cx + chip_w, cy + chip_h],
            fill=_ANNOTATION_COLOR,
            outline=_ANNOTATION_CHIP_OUTLINE,
            width=max(1, round(dsf)),
        )
        draw.text(
            (cx + pad - tb[0], cy + pad - tb[1]),
            label,
            font=font,
            fill=_ANNOTATION_TEXT_COLOR,
        )
    img.convert("RGB").save(target, "JPEG", quality=80)
    if not rows:
        return None
    return "\n".join(["n,ref,x,y,w,h", *rows])


_SEED_JUNK_ROOT: frozenset[str] = frozenset(
    {
        "component_crx_cache",
        "optimization_guide_model_store",
        "optimization_guide_prediction_model_downloads",
        "WasmTtsEngine",
        "Safe Browsing",
        "OnDeviceHeadSuggestModel",
        "ZxcvbnData",
        "GPUPersistentCache",
        "GraphiteDawnCache",
        "GrShaderCache",
        "ShaderCache",
        "CertificateRevocation",
        "Crashpad",
        "BrowserMetrics",
        "MEIPreload",
        "OptimizationHints",
        "extensions_crx_cache",
    }
)
_SEED_JUNK_PROFILE: frozenset[str] = frozenset(
    {
        "Cache",
        "Code Cache",
        "Media Cache",
        "GPUCache",
        "DawnWebGPUCache",
        "DawnGraphiteCache",
        "GraphiteDawnCache",
        "ShaderCache",
    }
)


def _slim_base_profile() -> None:
    base = base_profile_dir()
    with suppress(OSError):
        for name in _SEED_JUNK_ROOT:
            shutil.rmtree(base / name, ignore_errors=True)
        for profile in base.iterdir():
            if profile.is_dir() and (
                profile.name == "Default" or profile.name.startswith("Profile ")
            ):
                for name in _SEED_JUNK_PROFILE:
                    shutil.rmtree(profile / name, ignore_errors=True)


def _find_chrome_executable() -> str:
    candidates = [
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ]
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    msg = "No Chrome or Chromium executable found on PATH"
    raise RuntimeError(msg)


# See ADR-0032.
_BROWSER_TOKEN_RE = re.compile(r"[a-z]{5}")


def _record_profile_dir(browser_id: str) -> Path:
    return userscript_mod.browsers_root() / browser_id / "profile"


def _prepare_record_profile(browser_id: str) -> str:
    profile_dir = _record_profile_dir(browser_id)
    if not profile_dir.is_dir():
        profile_dir.mkdir(parents=True)
        base = base_profile_dir()
        if base.is_dir():
            with suppress(OSError):
                shutil.copytree(base, profile_dir, dirs_exist_ok=True)
    return str(profile_dir)


def _singleton_lock_pid(profile_dir: Path) -> int | None:
    with suppress(OSError):
        target = str((profile_dir / "SingletonLock").readlink())
        _, sep, pid_part = target.rpartition("-")
        if sep and pid_part.isdigit():
            return int(pid_part)
    return None


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _open_elsewhere(profile_dir: Path) -> bool:
    # See ADR-0032.
    pid = _singleton_lock_pid(profile_dir)
    return pid is not None and _process_alive(pid)


def init_chrome_profile() -> dict[str, Any]:
    chrome = _find_chrome_executable()
    profile_dir = base_profile_dir()

    if (profile_dir / "SingletonLock").exists():
        msg = (
            f"Profile directory {profile_dir} appears to be locked "
            "by another Chrome instance. Close it first."
        )
        raise RuntimeError(msg)

    profile_dir.mkdir(parents=True, exist_ok=True)

    args = [
        chrome,
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    subprocess.run(args, check=True)  # noqa: S603
    _slim_base_profile()

    return {"chrome": chrome, "profile_dir": str(profile_dir), "status": "closed"}


class BrowserOperationError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _is_target_closed_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "target closed" in msg or "has been closed" in msg


def _is_dialog_already_closed_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        "no dialog is showing" in msg
        or "already handled" in msg
        or "cannot accept dialog" in msg
    )


_DEPTH_NOTE = (
    "[snapshot capped at depth {depth} — tree is {real} levels deep, "
    "{hidden} lines hidden; raise depth to see everything, "
    "or page_find to search without the full tree]"
)

_PROP_LINE = re.compile(r"^\s*- /")
_BOX_TAG = re.compile(r"\s*\[box=[^\]]*\]")
_REF_TAG = re.compile(r"\s*\[ref=[^\]]*\]")
_DEEPER_TAG = re.compile(r"\s*\[deeper=\d+\]")

# See ADR-0026.
_NO_BASELINE_NOTE = (
    "(no previous snapshot for this depth — full tree stored as diff baseline)"
)
_NO_CHANGES = "(no changes since previous snapshot)"


def _diffable_lines(text: str) -> tuple[list[str], list[str]]:
    # See ADR-0025.
    raw: list[str] = []
    keys: list[str] = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("[snapshot capped"):
            continue
        key = _BOX_TAG.sub("", _REF_TAG.sub("", _DEEPER_TAG.sub("", line)))
        raw.append(line)
        keys.append(key)
    return raw, keys


def _ancestor_chain(lines: list[str], idx: int) -> list[str]:
    chain: list[str] = []
    indent = len(lines[idx]) - len(lines[idx].lstrip(" "))
    for line in reversed(lines[:idx]):
        if not line.strip():
            continue
        line_indent = len(line) - len(line.lstrip(" "))
        if line_indent < indent:
            crumb = line.strip().removeprefix("- ")
            chain.append(crumb.rstrip(":"))
            indent = line_indent
    chain.reverse()
    return chain


def _render_snapshot_diff(old: str, new: str) -> str | None:
    old_raw, old_keys = _diffable_lines(old)
    new_raw, new_keys = _diffable_lines(new)
    sm = difflib.SequenceMatcher(a=old_keys, b=new_keys, autojunk=False)
    hunks: list[list[str]] = []
    prev_equal = True
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            prev_equal = True
            continue
        lines = ["-" + ln for ln in old_raw[i1:i2]] + [
            "+" + ln for ln in new_raw[j1:j2]
        ]
        if prev_equal:
            source, idx = (new_raw, j1) if j2 > j1 else (old_raw, i1)
            header = " > ".join(_ancestor_chain(source, idx))
            hunks.append([f"[{len(hunks) + 1}] {header}", *lines])
        else:
            hunks[-1].extend(lines)
        prev_equal = False
    if not hunks:
        return None
    return "\n".join(line for hunk in hunks for line in hunk)


def _snapshot_error(e: BaseException) -> BrowserOperationError:
    return BrowserOperationError(f"Snapshot error: {e!s}")


def _raw_indent(line: str) -> int:
    stripped = line.lstrip()
    return len(line) - len(stripped)


def _content_indent(line: str) -> int | None:
    if _PROP_LINE.match(line):
        return None
    if not line.lstrip().startswith("- "):
        return None
    return _raw_indent(line)


def _max_content_depth(lines: list[str]) -> int:
    deepest = 0
    for line in lines:
        ind = _content_indent(line)
        if ind is not None:
            deepest = max(deepest, ind // 2)
    return deepest


def _align_lines(
    capped_lines: list[str], full_lines: list[str]
) -> dict[int, int] | None:

    def key(line: str) -> str:
        return _BOX_TAG.sub("", line).removesuffix(":")

    match: dict[int, int] = {}
    j = 0
    for i, line in enumerate(capped_lines):
        while j < len(full_lines) and key(full_lines[j]) != key(line):
            j += 1
        if j >= len(full_lines):
            return None
        match[i] = j
        j += 1
    return match


def _hidden_levels(full_lines: list[str], full_idx: int, boundary_indent: int) -> int:
    deeper = 0
    for line in full_lines[full_idx + 1 :]:
        if not line.lstrip().startswith("- ") or _raw_indent(line) <= boundary_indent:
            break
        ind = _content_indent(line)
        if ind is not None and ind > boundary_indent:
            deeper = max(deeper, (ind - boundary_indent) // 2)
    return deeper


def _annotate_capped_snapshot(capped: str, full: str, *, depth: int) -> str:
    capped_lines = capped.splitlines()
    full_lines = full.splitlines()
    match = _align_lines(capped_lines, full_lines)
    if match is None:
        return capped
    boundary_indent = depth * 2
    real = max(depth, _max_content_depth(full_lines))
    boundary: dict[int, int] = {}
    for i, line in enumerate(capped_lines):
        if _content_indent(line) == boundary_indent:
            deeper = _hidden_levels(full_lines, match[i], boundary_indent)
            if deeper:
                boundary[i] = deeper
    if not boundary:
        return capped
    tagged = list(capped_lines)
    for i, deeper in boundary.items():
        line = tagged[i]
        colon = line.endswith(":")
        body = line[:-1] if colon else line
        tagged[i] = f"{body} [deeper={deeper}]" + (":" if colon else "")
    hidden = len(full_lines) - len(capped_lines)
    note = _DEPTH_NOTE.format(depth=depth, real=real, hidden=hidden)
    return "\n".join(tagged) + "\n\n" + note


def _discard_task_result(task: asyncio.Task[Any] | None) -> None:
    # See ADR-0022.
    if task is None:
        return
    if task.done():
        with suppress(Exception):
            task.result()
    else:
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


class BrowserInstance:
    def __init__(
        self,
        browser_id: str,
        context: BrowserContext,
        display: VirtualDisplay,
    ) -> None:
        self.browser_id = browser_id
        self.context = context
        self.display = display
        # See ADR-0042.
        self.last_used = time.monotonic()
        # See ADR-0042.
        self.in_flight: int = 0
        self.closing: bool = False
        self._next_tab_id: int = 1
        self._tabs: dict[int, Page] = {}
        self._cdp_sessions: dict[int, CDPSession] = {}
        self._script_maps: dict[int, dict[str, str]] = {}
        self._coverage_recording: dict[int, bool] = {}
        self._coverage_accumulators: dict[int, dict[str, Any]] = {}
        # See ADR-0004.
        self._logpoints: dict[int, list[dict[str, Any]]] = {}
        self._logpoint_counter: int = 0
        # See ADR-0026.
        self._snapshot_baselines: dict[int, dict[int | None, str]] = {}
        self._browser_cdp_session: CDPSession | None = None
        # See ADR-0022.
        self._dialogs: dict[int, dict[str, Any]] = {}
        self._dialog_waiters: dict[int, list[asyncio.Future[None]]] = {}

        self.context.on("page", self._on_context_page)

    def _on_context_page(self, page: Page) -> None:
        if any(p is page for p in self._tabs.values()):
            return
        self._register_page(page)

    def _tab_id_for_page(self, page: Page) -> int:
        for tab_id, p in self._tabs.items():
            if p is page:
                return tab_id
        raise BrowserOperationError(
            f"Page was not registered in browser {self.browser_id}."
        )

    def _register_page(self, page: Page) -> int:
        tab_id = self._next_tab_id
        self._next_tab_id += 1
        self._tabs[tab_id] = page
        self._script_maps[tab_id] = {}
        page.on("framenavigated", lambda frame: self._on_frame_navigated(tab_id, frame))
        page.on("close", lambda: self._on_page_close(tab_id))
        page.on("dialog", lambda dialog: self._on_dialog_open(tab_id, dialog))
        with suppress(RuntimeError):
            asyncio.get_running_loop().create_task(
                self._setup_cdp_session(tab_id, page)
            )
        return tab_id

    def _on_frame_navigated(self, tab_id: int, frame) -> None:
        # See ADR-0005, ADR-0026.
        self._snapshot_baselines.pop(tab_id, None)
        if frame.parent_frame is None:
            self._script_maps.get(tab_id, {}).clear()

    def _on_page_close(self, tab_id: int) -> None:
        cdp = self._cdp_sessions.pop(tab_id, None)
        if cdp is not None:
            task = asyncio.get_running_loop().create_task(cdp.detach())
            task.add_done_callback(_discard_task_result)
        if tab_id in self._dialogs:
            self._clear_dialog_entry(tab_id)
        self._dialog_waiters.pop(tab_id, None)
        self._tabs.pop(tab_id, None)
        self._script_maps.pop(tab_id, None)
        self._coverage_recording.pop(tab_id, None)
        self._coverage_accumulators.pop(tab_id, None)
        self._snapshot_baselines.pop(tab_id, None)
        # See ADR-0004.
        self._logpoints.pop(tab_id, None)

    async def _setup_cdp_session(self, tab_id: int, page: Page) -> None:
        script_map = self._script_maps.setdefault(tab_id, {})

        def _on_script_parsed(event: dict[str, Any]) -> None:
            script_map[event.get("scriptId", "")] = event.get("url", "")

        try:
            cdp = await self.context.new_cdp_session(page)
            await cdp.send("Debugger.enable")
            cdp.on("Debugger.scriptParsed", _on_script_parsed)
            self._cdp_sessions[tab_id] = cdp
        except Exception as exc:
            logger.warning("Failed to set up CDP session for tab %s: %s", tab_id, exc)

    async def _get_cdp_session(self, tab_id: int) -> CDPSession:
        self._require_tab(tab_id)
        cdp = self._cdp_sessions.get(tab_id)
        if cdp is None:
            page = self._tabs[tab_id]
            await self._setup_cdp_session(tab_id, page)
            cdp = self._cdp_sessions.get(tab_id)
            if cdp is None:
                raise BrowserOperationError(
                    f"Failed to set up CDP session for tab {tab_id}."
                )
        return cdp

    def _require_tab(self, tab_id: int) -> Page:
        page = self._tabs.get(tab_id)
        if page is None or page.is_closed():
            self._tabs.pop(tab_id, None)
            raise BrowserOperationError(
                f"Tab {tab_id} not found in browser {self.browser_id}."
            )
        return page

    def _require_ready_tab(self, tab_id: int) -> Page:
        # See ADR-0022.
        page = self._require_tab(tab_id)
        self._reject_if_dialog(tab_id)
        return page

    async def _load_userscript_extension(self) -> str | None:
        if self._browser_cdp_session is None:
            browser = self.context.browser
            if browser is None:
                return None
            self._browser_cdp_session = await browser.new_browser_cdp_session()
        ext_path = str(userscript_mod.sync_extension(self.browser_id))
        try:
            resp = await self._browser_cdp_session.send(
                "Extensions.loadUnpacked", {"path": ext_path}
            )
        except Exception as exc:
            logger.warning("Failed to load userscript extension: %s", exc)
            return None
        ext_id = resp.get("id")
        self._extension_id = ext_id
        return ext_id

    async def _run_action(
        self,
        tab_id: int,
        verb: str,
        action: Callable[[], Awaitable[Any]],
        convert: Callable[[Exception], BrowserOperationError | None] | None = None,
    ) -> Any:
        try:
            return await action()
        except Exception as exc:
            if _is_target_closed_error(exc):
                self._on_page_close(tab_id)
                raise BrowserOperationError(
                    f"Tab {tab_id} closed during {verb}."
                ) from exc
            if convert is not None:
                converted = convert(exc)
                if converted is not None:
                    raise converted from exc
            raise

    # See ADR-0022.

    def _on_dialog_open(self, tab_id: int, dialog: Dialog) -> None:
        previous = self._dialogs.get(tab_id)
        if previous is not None:
            _discard_task_result(previous.get("probe_task"))
        entry = {
            "type": dialog.type,
            "message": dialog.message,
            "default_value": dialog.default_value,
        }
        page = self._tabs.get(tab_id)
        probe_task = None
        if page is not None and not page.is_closed():
            probe_task = asyncio.create_task(page.evaluate("1"))
            probe_task.add_done_callback(
                lambda _task: self._on_probe_done(tab_id, probe_task)
            )
        self._dialogs[tab_id] = {
            **entry,
            "dialog": dialog,
            "probe_task": probe_task,
        }
        waiters = self._dialog_waiters.pop(tab_id, None)
        if waiters:
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(None)

    def _on_probe_done(self, tab_id: int, probe_task: asyncio.Task[Any]) -> None:
        with suppress(Exception):
            probe_task.result()
        entry = self._dialogs.get(tab_id)
        if entry is None or entry.get("probe_task") is not probe_task:
            return
        self._clear_dialog_entry(tab_id)
        asyncio.get_running_loop().create_task(self._purge_dialog_state(entry))

    def _clear_dialog_entry(self, tab_id: int) -> None:
        self._dialogs.pop(tab_id, None)

    async def _purge_dialog_state(self, entry: dict[str, Any]) -> None:
        dialog = entry.get("dialog")
        if dialog is not None:
            with suppress(Exception):
                await dialog.accept()

    def _reject_if_dialog(self, tab_id: int) -> None:
        entry = self._dialogs.get(tab_id)
        if entry is not None:
            raise BrowserOperationError(
                f"Tab {tab_id} has an open {entry['type']} dialog "
                f"({entry['message']!r}) — handle it with dialog_handle, "
                "then retry this call."
            )

    async def _race_dialog(
        self, tab_id: int, action: Callable[[], Awaitable[Any]]
    ) -> tuple[bool, Any]:
        # See ADR-0022.
        waiter = asyncio.get_running_loop().create_future()
        self._dialog_waiters.setdefault(tab_id, []).append(waiter)
        action_task = asyncio.create_task(action())
        action_task.add_done_callback(_discard_task_result)
        try:
            done, _pending = await asyncio.wait(
                {action_task, waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if waiter in done:
                return True, await self._open_dialog_result(tab_id)
            return False, await asyncio.shield(action_task)
        finally:
            waiters = self._dialog_waiters.get(tab_id)
            if waiters is not None:
                with suppress(ValueError):
                    waiters.remove(waiter)
                if not waiters:
                    self._dialog_waiters.pop(tab_id, None)

    @staticmethod
    def _dialog_details(entry: dict[str, Any], tab_id: int) -> dict[str, Any]:
        return {
            "type": entry["type"],
            "message": entry["message"],
            "default_value": entry["default_value"],
            "tab_id": tab_id,
        }

    async def _open_dialog_result(self, tab_id: int) -> dict[str, Any]:
        entry = self._dialogs.get(tab_id)
        if entry is None:
            raise BrowserOperationError(
                f"Dialog race resolved for tab {tab_id} but no dialog "
                "is registered — this is a bug in odda's dialog engine."
            )
        return {"dialog": self._dialog_details(entry, tab_id)}

    def open_dialogs(self) -> list[dict[str, Any]]:
        return [
            self._dialog_details(entry, tab_id)
            for tab_id, entry in self._dialogs.items()
        ]

    async def handle_dialog(
        self, tab_id: int, action: str, prompt_text: str | None = None
    ) -> dict[str, Any]:
        # See ADR-0022.
        if action not in ("accept", "dismiss"):
            raise BrowserOperationError(
                f"Invalid dialog action {action!r}; use 'accept' or 'dismiss'."
            )
        entry = self._dialogs.get(tab_id)
        if entry is None:
            raise BrowserOperationError(
                f"Tab {tab_id} in browser {self.browser_id} has no open dialog."
            )
        dialog = entry["dialog"]
        try:
            if action == "accept":
                text = prompt_text
                if text is None and entry["type"] == "prompt":
                    text = entry["default_value"]
                await dialog.accept(text)
            else:
                await dialog.dismiss()
        except Exception as exc:
            if _is_target_closed_error(exc):
                raise BrowserOperationError(
                    f"Tab {tab_id} closed while handling its dialog."
                ) from exc
            if _is_dialog_already_closed_error(exc):
                raise BrowserOperationError(
                    f"Dialog on tab {tab_id} was already closed "
                    "(handled by the user or closed in the browser)."
                ) from exc
            raise
        if self._dialogs.get(tab_id) is entry:
            self._clear_dialog_entry(tab_id)
        return {"handled": True, "action": action, "tab_id": tab_id}

    async def _reload_userscript_extension(self) -> str | None:
        if self._browser_cdp_session is not None and self._extension_id:
            with suppress(Exception):
                await self._browser_cdp_session.send(
                    "Extensions.uninstall", {"id": self._extension_id}
                )
            self._extension_id = None
        return await self._load_userscript_extension()

    async def list_tabs(self) -> list[dict]:
        tabs = []
        for tab_id, page in self._tabs.items():
            if page.is_closed():
                continue
            title = ""
            with suppress(Exception):
                title = await page.title()
            tabs.append(
                {
                    "tab_id": tab_id,
                    "url": page.url,
                    "title": title,
                }
            )
        return tabs

    async def open_tab(self, url: str | None = None) -> dict[str, Any] | int:
        # See ADR-0022.
        page = await self.context.new_page()
        tab_id = self._tab_id_for_page(page)
        if url is None:
            return tab_id
        won, result = await self._race_dialog(tab_id, lambda: page.goto(url))
        return result if won else tab_id

    async def close_tab(self, tab_id: int) -> None:
        page = self._require_tab(tab_id)
        with suppress(Exception):
            with suppress(TimeoutError):
                await asyncio.wait_for(page.close(), 10)
            if not page.is_closed():
                with suppress(TimeoutError):
                    await asyncio.wait_for(page.close(), 5)
        self._on_page_close(tab_id)

    async def navigate(
        self,
        tab_id: int,
        url: str,
        *,
        timeout_ms: float = 30000.0,
        wait_until: str = "load",
    ) -> dict[str, Any]:
        # See ADR-0022.
        page = self._require_ready_tab(tab_id)

        def _convert(exc: Exception) -> BrowserOperationError | None:
            msg = str(exc)
            if "timeout" in msg.lower() or "Timeout" in type(exc).__name__:
                hint = (
                    " For SPAs that don't fire `load`, use the `wait_for`"
                    " tool with a JS expression after navigate to poll"
                    " for a condition."
                )
                return BrowserOperationError(
                    f"Failed to navigate: {msg} (wait-until `{wait_until}`){hint}"
                )
            return BrowserOperationError(f"Failed to navigate: {msg}")

        won, result = await self._race_dialog(
            tab_id,
            lambda: self._run_action(
                tab_id,
                "navigation",
                lambda: page.goto(url, timeout=timeout_ms, wait_until=wait_until),
                _convert,
            ),
        )
        return result if won else {"status": f"Navigated to: {url}"}

    async def eval_js(self, tab_id: int, js_code: str) -> Any:
        # See ADR-0022.
        page = self._require_ready_tab(tab_id)
        _won, result = await self._race_dialog(
            tab_id,
            lambda: self._run_action(
                tab_id,
                "eval",
                lambda: page.evaluate(js_code, isolated_context=False),
                lambda exc: BrowserOperationError(f"JavaScript error: {exc!s}"),
            ),
        )
        return result

    async def wait_for(self, tab_id: int, expression: str, *, timeout_ms: float) -> Any:
        page = self._require_ready_tab(tab_id)
        fn = (
            "() => { try { const v = (" + expression + "); return v ? v : false; }"
            " catch (e) { return false; } }"
        )

        async def _wait() -> Any:
            handle = await self._run_action(
                tab_id,
                "wait-for",
                lambda: page.wait_for_function(fn, timeout=timeout_ms),
                lambda exc: (
                    BrowserOperationError(
                        f"Timeout after {timeout_ms}ms waiting for expression "
                        "to become truthy"
                    )
                    if "timeout" in str(exc).lower() or "Timeout" in type(exc).__name__
                    else None
                ),
            )
            try:
                return await handle.json_value()
            except Exception:
                return str(handle)

        _won, result = await self._race_dialog(tab_id, _wait)
        return result

    async def screenshot(
        self, tab_id: int, output_path: str | None = None, *, annotate: bool = False
    ) -> tuple[str, str | None]:
        # See ADR-0028.
        page = self._require_ready_tab(tab_id)
        if output_path is not None:
            target = Path(output_path)
            target.parent.mkdir(parents=True, exist_ok=True)
        else:
            target = Path(tempfile.gettempdir()) / f"screenshot_{int(time.time())}.jpeg"

        async def _shoot() -> tuple[str, str | None]:
            if not annotate:
                await page.screenshot(path=str(target), type="jpeg", full_page=False)
                return str(target), None
            # See ADR-0026.
            png = await page.screenshot(type="png", full_page=False)
            dsf = await page.evaluate("window.devicePixelRatio")
            if not isinstance(dsf, (int, float)) or dsf <= 0:
                dsf = 1.0
            snapshot_text = await page.aria_snapshot(mode="ai", depth=None, boxes=True)
            legend = _draw_annotations(
                Image.open(BytesIO(png)), snapshot_text, float(dsf), target
            )
            return str(target), legend

        return await self._run_action(
            tab_id,
            "screenshot",
            _shoot,
            lambda e: BrowserOperationError(f"Screenshot error: {e!s}"),
        )

    async def page_snapshot(
        self,
        tab_id: int,
        *,
        depth: int | None = None,
        boxes: bool = False,
        diff: bool = False,
    ) -> str:
        # See ADR-0026.
        text = await self._render_snapshot(tab_id, depth=depth, boxes=boxes)
        key: int | None = depth if depth and depth >= 1 else None
        baselines = self._snapshot_baselines.setdefault(tab_id, {})
        baseline = baselines.get(key)
        baselines[key] = text
        if not diff:
            return text
        if baseline is None:
            return f"{_NO_BASELINE_NOTE}\n{text}"
        return _render_snapshot_diff(baseline, text) or _NO_CHANGES

    async def _render_snapshot(
        self, tab_id: int, *, depth: int | None = None, boxes: bool = False
    ) -> str:
        # See ADR-0026.
        page = self._require_ready_tab(tab_id)
        text = await self._run_action(
            tab_id,
            "snapshot",
            lambda: page.aria_snapshot(mode="ai", depth=depth, boxes=boxes or None),
            _snapshot_error,
        )
        if not depth or depth < 1:
            return text
        if _max_content_depth(text.splitlines()) < depth:
            return text
        try:
            full = await self._run_action(
                tab_id,
                "snapshot",
                lambda: page.aria_snapshot(mode="ai", boxes=None),
                _snapshot_error,
            )
        except Exception:
            return text
        return _annotate_capped_snapshot(text, full, depth=depth)

    async def page_find(
        self, tab_id: int, pattern: re.Pattern[str], *, boxes: bool = False
    ) -> str:
        # See ADR-0026.
        text = await self._render_snapshot(tab_id, boxes=boxes)
        lines = text.splitlines()
        matched = [i for i, line in enumerate(lines) if pattern.search(line)]
        if not matched:
            return f"No matches for /{pattern.pattern}/ in snapshot"

        windows: list[tuple[int, int]] = []
        for i in matched:
            lo = max(0, i - _FIND_CONTEXT_LINES)
            hi = min(len(lines) - 1, i + _FIND_CONTEXT_LINES)
            if windows and lo <= windows[-1][1] + 1:
                windows[-1] = (windows[-1][0], hi)
            else:
                windows.append((lo, hi))

        blocks = []
        for w, (lo, hi) in enumerate(windows, 1):
            first_match = next(i for i in matched if lo <= i <= hi)
            chain = _ancestor_chain(lines, first_match)
            body = "\n".join(lines[lo : hi + 1])
            blocks.append(f"[{w}] {' > '.join(chain)}\n{body}")
        return "\n\n".join(blocks)

    def _ref_convert(
        self, ref: str, verb: str, timeout_ms: float
    ) -> Callable[[Exception], BrowserOperationError | None]:

        def convert(exc: Exception) -> BrowserOperationError | None:
            if "timeout" in str(exc).lower() or "Timeout" in type(exc).__name__:
                return BrowserOperationError(
                    f"ref {ref} did not resolve or become actionable "
                    f"within {timeout_ms}ms (the element may have been "
                    f"removed, or it may be disabled or covered by an "
                    f"overlay; take a new snapshot if stale)"
                )
            return BrowserOperationError(f"{verb.capitalize()} error: {exc!s}")

        return convert

    async def _ref_tool(
        self,
        tab_id: int,
        ref: str,
        verb: str,
        action: Callable[[], Awaitable[Any]],
        status: dict[str, Any],
        *,
        timeout_ms: float,
    ) -> dict[str, Any]:
        # See ADR-0022.
        self._require_ready_tab(tab_id)
        won, result = await self._race_dialog(
            tab_id,
            lambda: self._run_action(
                tab_id, verb, action, self._ref_convert(ref, verb, timeout_ms)
            ),
        )
        return result if won else status

    async def page_click(
        self, tab_id: int, ref: str, *, timeout_ms: float
    ) -> dict[str, Any]:
        locator = self._require_ready_tab(tab_id).locator(f"aria-ref={ref}")
        return await self._ref_tool(
            tab_id,
            ref,
            "click",
            lambda: locator.click(timeout=timeout_ms),
            {"status": "clicked", "ref": ref},
            timeout_ms=timeout_ms,
        )

    async def page_click_coords(
        self, tab_id: int, *, x: float, y: float
    ) -> dict[str, Any]:
        page = self._require_ready_tab(tab_id)
        won, _ = await self._race_dialog(
            tab_id,
            lambda: self._run_action(
                tab_id,
                "click",
                lambda: page.mouse.click(x, y),
                lambda e: BrowserOperationError(f"Click error: {e!s}"),
            ),
        )
        if won:
            return await self._open_dialog_result(tab_id)
        return {"status": "clicked", "x": x, "y": y}

    async def page_hover_coords(
        self, tab_id: int, *, x: float, y: float
    ) -> dict[str, Any]:
        page = self._require_ready_tab(tab_id)
        won, _ = await self._race_dialog(
            tab_id,
            lambda: self._run_action(
                tab_id,
                "hover",
                lambda: page.mouse.move(x, y),
                lambda e: BrowserOperationError(f"Hover error: {e!s}"),
            ),
        )
        if won:
            return await self._open_dialog_result(tab_id)
        return {"status": "hovered", "x": x, "y": y}

    async def page_fill(
        self, tab_id: int, ref: str, value: str, *, timeout_ms: float
    ) -> dict[str, Any]:
        locator = self._require_ready_tab(tab_id).locator(f"aria-ref={ref}")
        return await self._ref_tool(
            tab_id,
            ref,
            "fill",
            lambda: locator.fill(value, timeout=timeout_ms),
            {"status": "filled", "ref": ref},
            timeout_ms=timeout_ms,
        )

    async def page_hover(
        self, tab_id: int, ref: str, *, timeout_ms: float
    ) -> dict[str, Any]:
        locator = self._require_ready_tab(tab_id).locator(f"aria-ref={ref}")
        return await self._ref_tool(
            tab_id,
            ref,
            "hover",
            lambda: locator.hover(timeout=timeout_ms),
            {"status": "hovered", "ref": ref},
            timeout_ms=timeout_ms,
        )

    async def page_upload(
        self, tab_id: int, ref: str, files: list[str], *, timeout_ms: float
    ) -> dict[str, Any]:
        locator = self._require_ready_tab(tab_id).locator(f"aria-ref={ref}")
        return await self._ref_tool(
            tab_id,
            ref,
            "upload",
            lambda: locator.set_input_files(files, timeout=timeout_ms),
            {"status": "uploaded", "ref": ref, "files": files},
            timeout_ms=timeout_ms,
        )

    async def list_event_listeners(self, tab_id: int) -> list[dict]:
        self._require_ready_tab(tab_id)
        cdp = await self._get_cdp_session(tab_id)
        script_map = self._script_maps.get(tab_id, {})

        listeners: list[dict] = []
        for element_tag, expression in (("window", "window"), ("document", "document")):
            ref = await cdp.send(
                "Runtime.evaluate",
                {"expression": expression, "includeCommandLineAPI": True},
            )
            object_id = ref.get("result", {}).get("objectId")
            if not object_id:
                continue

            response = await cdp.send(
                "DOMDebugger.getEventListeners",
                {"objectId": object_id},
            )
            listeners.extend(
                {
                    "type": listener.get("type", ""),
                    "element_tag": element_tag,
                    "line_number": listener.get("lineNumber"),
                    "column_number": listener.get("columnNumber"),
                    "script_url": script_map.get(listener.get("scriptId")),
                }
                for listener in response.get("listeners", [])
            )

        return listeners

    async def cookies_list(self, url: str | None = None) -> list[dict[str, Any]]:
        try:
            return await self.context.cookies([url] if url is not None else None)
        except Exception as e:
            raise BrowserOperationError(f"Failed to list cookies: {e!s}") from e

    async def cookies_clear(
        self,
        name: str | None = None,
        domain: str | None = None,
        path: str | None = None,
    ) -> dict[str, Any]:

        def _key(c: dict[str, Any]) -> tuple[str, str, str]:
            return (c["name"], c["domain"], c["path"])

        try:
            before = {_key(c) for c in await self.context.cookies()}
            await self.context.clear_cookies(name=name, domain=domain, path=path)
            after = {_key(c) for c in await self.context.cookies()}
        except Exception as e:
            raise BrowserOperationError(f"Failed to clear cookies: {e!s}") from e
        return {"cleared": len(before - after)}

    async def cookies_set(self, cookies: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            await self.context.add_cookies(cookies)
        except Exception as e:
            raise BrowserOperationError(f"Failed to set cookies: {e!s}") from e
        return {"set": len(cookies)}

    async def coverage_start(self, tab_id: int) -> dict[str, Any]:
        self._require_ready_tab(tab_id)
        if self._coverage_recording.get(tab_id):
            raise BrowserOperationError(f"Tab {tab_id} is already recording coverage.")
        cdp = await self._get_cdp_session(tab_id)
        await coverage_mod.start(cdp)
        self._coverage_accumulators[tab_id] = coverage_mod.new_accumulator()
        self._coverage_recording[tab_id] = True
        return {"status": "recording"}

    async def coverage_snapshot(self, tab_id: int) -> dict[str, Any]:
        self._require_ready_tab(tab_id)
        if not self._coverage_recording.get(tab_id):
            raise BrowserOperationError(f"Tab {tab_id} is not recording coverage.")
        cdp = await self._get_cdp_session(tab_id)
        script_map = self._script_maps.get(tab_id, {})
        delta = await coverage_mod.take_precise_coverage(cdp, script_map)
        accumulator = self._coverage_accumulators.get(tab_id)
        if accumulator is not None:
            coverage_mod.merge_delta(accumulator, delta)
        return delta

    async def coverage_stop(self, tab_id: int) -> dict[str, Any]:
        self._require_ready_tab(tab_id)
        if not self._coverage_recording.get(tab_id):
            raise BrowserOperationError(f"Tab {tab_id} is not recording coverage.")
        cdp = await self._get_cdp_session(tab_id)
        script_map = self._script_maps.get(tab_id, {})
        delta = await coverage_mod.take_precise_coverage(cdp, script_map)
        accumulator = self._coverage_accumulators.pop(tab_id, None)
        if accumulator is None:
            accumulator = coverage_mod.new_accumulator()
        coverage_mod.merge_delta(accumulator, delta)
        await coverage_mod.stop(cdp)
        self._coverage_recording.pop(tab_id, None)
        return coverage_mod.format_accumulated(accumulator)

    async def wrap_dump(
        self, tab_id: int, name: str | None = None
    ) -> list[dict[str, Any]]:
        self._require_tab(tab_id)
        result = await self.eval_js(
            tab_id,
            "typeof window.__oddaWrap === 'undefined' ? [] : window.__oddaWrap",
        )
        if not isinstance(result, list):
            return []
        if name is not None:
            return [r for r in result if isinstance(r, dict) and r.get("wrap") == name]
        return result

    async def wrap_clear(self, tab_id: int) -> dict[str, Any]:
        self._require_tab(tab_id)
        prev = await self.eval_js(
            tab_id,
            "typeof window.__oddaWrap === 'undefined' ? 0 : window.__oddaWrap.length",
        )
        await self.eval_js(tab_id, "window.__oddaWrap = []")
        return {"status": "cleared", "count": prev if isinstance(prev, int) else 0}

    async def logpoint_add(
        self, tab_id: int, url: str, line: int, col: int, expr: str
    ) -> dict[str, Any]:
        self._require_tab(tab_id)
        cdp = await self._get_cdp_session(tab_id)
        script_map = self._script_maps.get(tab_id, {})
        registry = self._logpoints.setdefault(tab_id, [])
        existing = next(
            (
                lp
                for lp in registry
                if lp["url"] == url and lp["line"] == line and lp["col"] == col
            ),
            None,
        )
        if existing is not None:
            raise BrowserOperationError(
                f"A logpoint already exists at {url}:{line}:{col} "
                f"(id={existing['id']}); remove it first."
            )
        self._logpoint_counter += 1
        lp_id = f"lp-{self._logpoint_counter}"
        result = await logpoint_mod.add(cdp, lp_id, url, line, col, expr, script_map)
        registry.append(
            {
                "id": lp_id,
                "cdp_id": result.get("cdp_id", ""),
                "url": url,
                "line": line,
                "col": col,
                "expr": expr,
            }
        )
        return result

    async def logpoint_list(self, tab_id: int) -> list[dict[str, Any]]:
        self._require_tab(tab_id)
        return [
            {
                "id": lp["id"],
                "url": lp["url"],
                "line": lp["line"],
                "col": lp["col"],
                "expr": lp["expr"],
            }
            for lp in self._logpoints.get(tab_id, [])
        ]

    async def logpoint_dump(self, tab_id: int) -> list[dict[str, Any]]:
        # See ADR-0004.
        self._require_tab(tab_id)
        result = await self.eval_js(
            tab_id,
            "typeof window.__oddaLogpoint === 'undefined' ? [] : window.__oddaLogpoint",
        )
        if isinstance(result, list):
            return result
        return []

    async def logpoint_clear(self, tab_id: int) -> dict[str, Any]:
        self._require_tab(tab_id)
        prev = await self.eval_js(
            tab_id,
            "typeof window.__oddaLogpoint === 'undefined'"
            " ? 0 : window.__oddaLogpoint.length",
        )
        await self.eval_js(tab_id, "window.__oddaLogpoint = []")
        return {"status": "cleared", "count": prev if isinstance(prev, int) else 0}

    async def logpoint_remove(self, tab_id: int, lp_id: str) -> dict[str, Any]:
        self._require_tab(tab_id)
        registry = self._logpoints.get(tab_id, [])
        entry = next((lp for lp in registry if lp["id"] == lp_id), None)
        if entry is None:
            raise BrowserOperationError(f"Logpoint {lp_id} not found in tab {tab_id}.")
        cdp = await self._get_cdp_session(tab_id)
        cdp_id = entry.get("cdp_id", "")
        if cdp_id:
            with suppress(Exception):
                await logpoint_mod.remove(cdp, cdp_id)
        self._logpoints[tab_id] = [lp for lp in registry if lp["id"] != lp_id]
        return {"status": "removed", "id": lp_id}

    async def _close(self) -> None:
        for tab_id in list(self._cdp_sessions.keys()):
            if tab_id in self._dialogs:
                continue
            with suppress(Exception):
                await asyncio.wait_for(self._cdp_sessions[tab_id].detach(), 5.0)
        self._cdp_sessions.clear()
        self._tabs.clear()
        self._dialogs.clear()
        self._script_maps.clear()
        if self.context:
            closed = False
            with suppress(Exception, TimeoutError):
                await asyncio.wait_for(self.context.close(), 15)
                closed = True
            if not closed and self.display:
                await self.display.close()
                with suppress(Exception, TimeoutError):
                    await asyncio.wait_for(self.context.close(), 10)
        self.context = None  # type: ignore[assignment]
        if self.display:
            await self.display.close()
            self.display = None  # type: ignore[assignment]


_DRIVERS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Playwright] = (
    weakref.WeakKeyDictionary()
)
_DRIVER_STARTS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Task[Playwright]
] = weakref.WeakKeyDictionary()

_WARM_REFS: set[asyncio.Task[Playwright]] = set()


async def _start_driver() -> Playwright:
    driver = await async_playwright().start()
    _DRIVERS[asyncio.get_running_loop()] = driver
    return driver


async def _driver() -> Playwright:
    loop = asyncio.get_running_loop()
    driver = _DRIVERS.get(loop)
    if driver is not None:
        return driver
    task = _DRIVER_STARTS.get(loop)
    if task is None:
        task = asyncio.create_task(_start_driver())
        _DRIVER_STARTS[loop] = task
    try:
        return await asyncio.shield(task)
    except Exception:
        if _DRIVER_STARTS.get(loop) is task:
            _DRIVER_STARTS.pop(loop)
        raise


def _warm_done(task: asyncio.Task[Playwright]) -> None:
    _WARM_REFS.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("Driver warm-up failed: %s", task.exception())


def warm_driver() -> None:
    task = asyncio.create_task(_driver())
    _WARM_REFS.add(task)
    task.add_done_callback(_warm_done)


class BrowserManager:
    def __init__(self, proxy=None, config: OddaConfig | None = None):
        self._instances: dict[str, BrowserInstance] = {}
        self.proxy = proxy
        self.config = config if config is not None else OddaConfig()
        # See ADR-0042.
        self._opening: set[str] = set()

    def _new_browser_id(self) -> str:
        # See ADR-0030.
        browsers_root = userscript_mod.browsers_root()
        while True:
            token = "".join(secrets.choice(string.ascii_lowercase) for _ in range(5))
            if (
                token in self._instances
                or token in self._opening
                or (browsers_root / token).exists()
            ):
                continue
            return token

    def list_instances(self) -> list[dict[str, Any]]:
        # See ADR-0032.
        browsers_root = userscript_mod.browsers_root()
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for bid, inst in self._instances.items():
            seen.add(bid)
            rows.append(
                {
                    "browser_id": bid,
                    "state": "open",
                    "tab_count": len(inst._tabs),  # noqa: SLF001
                    "vnc_url": inst.display.vnc_url,
                    "novnc_url": inst.display.novnc_url,
                }
            )
        with suppress(OSError):
            for entry in browsers_root.iterdir():
                token = entry.name
                if token in seen or not _BROWSER_TOKEN_RE.fullmatch(token):
                    continue
                profile_dir = _record_profile_dir(token)
                if not profile_dir.is_dir():
                    continue
                state = (
                    "open in another session"
                    if _open_elsewhere(profile_dir)
                    else "closed"
                )
                rows.append({"browser_id": token, "state": state})
        rows.sort(key=lambda row: row["browser_id"])
        return rows

    async def close_all(self) -> None:
        await asyncio.gather(
            *(self.close_instance(bid) for bid in list(self._instances))
        )

    async def run_stale_reaper(self, timeout_s: int) -> None:
        # See ADR-0042.
        while True:
            now = time.monotonic()
            for browser_id in list(self._instances):
                inst = self._instances.get(browser_id)
                if (
                    inst is None
                    or inst.closing
                    or inst.in_flight > 0
                    or inst.last_used + timeout_s > now
                ):
                    continue
                try:
                    await self.close_instance(browser_id)
                except Exception:
                    logger.warning(
                        "stale close of browser %s failed; retrying next sweep",
                        browser_id,
                        exc_info=True,
                    )
            deadlines = [
                inst.last_used + timeout_s
                for inst in self._instances.values()
                if not inst.closing and inst.in_flight == 0
            ]
            delay = (
                max(0.0, min(deadlines) - time.monotonic())
                if deadlines
                else float(timeout_s)
            )
            await asyncio.sleep(delay)

    def _require_instance(self, browser_id: str) -> BrowserInstance:
        # See ADR-0030.
        bid = browser_id.lower()
        inst = self._instances.get(bid)
        if inst is None:
            if _record_profile_dir(bid).is_dir():
                raise BrowserOperationError(f"Browser {browser_id} is not open.")
            raise BrowserOperationError(f"Browser {browser_id} not found.")
        if inst.closing:
            raise BrowserOperationError(f"Browser {browser_id} is closing.")
        return inst

    @asynccontextmanager
    async def _targeted(self, browser_id: str) -> AsyncIterator[BrowserInstance]:
        # See ADR-0042.
        inst = self._require_instance(browser_id)
        inst.last_used = time.monotonic()
        inst.in_flight += 1
        try:
            yield inst
        finally:
            inst.in_flight -= 1
            inst.last_used = time.monotonic()

    @staticmethod
    async def _seed_proxy_auth(initial_page: Page) -> None:
        # See ADR-0031.
        with suppress(Exception):
            await asyncio.wait_for(
                initial_page.evaluate(f"fetch('{AUTH_CANARY_URL}').catch(() => 0)"),
                15,
            )

    async def _create_instance(self, browser_id: str) -> BrowserInstance:
        # See ADR-0032.
        # See ADR-0031.
        proxy_config = None
        if self.proxy:
            proxy_config = {
                "server": self.proxy.proxy_url,
                "username": browser_id,
                "password": PROXY_AUTH_PASSWORD,
            }

        chrome_executable = _find_chrome_executable()

        # See ADR-0040.
        display_task = asyncio.create_task(
            start_virtual_display(SCREEN_WIDTH, SCREEN_HEIGHT, self.config.vnc_bind)
        )
        try:
            user_data_dir, playwright = await asyncio.gather(
                asyncio.to_thread(_prepare_record_profile, browser_id),
                _driver(),
            )
        except BaseException:
            display_task.cancel()
            try:
                started = await display_task
            except BaseException:
                started = None
            if started is not None:
                await started.close()
            raise
        display = await display_task
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                executable_path=chrome_executable,
                headless=False,
                # See ADR-0039.
                no_viewport=True,
                proxy=proxy_config,
                ignore_https_errors=True,
                ignore_default_args=True,
                env={
                    **{k: v for k, v in os.environ.items() if k != "WAYLAND_DISPLAY"},
                    **display.env,
                    "XDG_SESSION_TYPE": "x11",
                },
                args=build_chrome_args(
                    user_data_dir,
                    self.proxy.proxy_url if self.proxy else None,
                ),
            )
        except BaseException:
            await display.close()
            raise
        instance = BrowserInstance(browser_id, context, display)

        initial_page = context.pages[0] if context.pages else await context.new_page()
        if not any(p is initial_page for p in instance._tabs.values()):  # noqa: SLF001
            instance._register_page(initial_page)  # noqa: SLF001

        await asyncio.gather(
            instance._load_userscript_extension(),  # noqa: SLF001
            self._seed_proxy_auth(initial_page),
        )

        self._instances[browser_id] = instance
        return instance

    async def open(self, *, browser_id: str | None = None) -> dict[str, Any]:
        # See ADR-0032, ADR-0040.
        bid = browser_id.lower() if browser_id is not None else self._new_browser_id()
        # See ADR-0030.
        if bid in self._instances:
            raise BrowserOperationError(f"Browser {bid} is already open.")
        if bid in self._opening:
            raise BrowserOperationError(f"Browser {bid} is already opening.")
        limit = self.config.max_browsers
        if limit and len(self._instances) + len(self._opening) >= limit:
            raise BrowserOperationError(
                f"Browser limit reached ({limit} open). "
                "Close a browser before opening another."
            )
        self._opening.add(bid)
        try:
            try:
                if browser_id is None:
                    instance = await self._create_instance(bid)
                else:
                    instance = await self._reopen_instance(bid)
            except BrowserOperationError:
                raise
            except Exception as e:
                raise BrowserOperationError(f"Failed to open browser: {e!s}") from e
        finally:
            self._opening.discard(bid)
        initial_tab_id = next(iter(instance._tabs))  # noqa: SLF001
        return {
            "browser_id": instance.browser_id,
            "tab_id": initial_tab_id,
            "status": "launched",
            "display": instance.display.display,
            "vnc_url": instance.display.vnc_url,
            "novnc_url": instance.display.novnc_url,
        }

    async def _reopen_instance(self, browser_id: str) -> BrowserInstance:
        # See ADR-0032.
        profile_dir = _record_profile_dir(browser_id)
        if not profile_dir.is_dir():
            raise BrowserOperationError(f"Browser {browser_id} not found.")
        if _open_elsewhere(profile_dir):
            raise BrowserOperationError(
                f"Browser {browser_id} is open in another session."
            )
        return await self._create_instance(browser_id=browser_id)

    async def close_instance(self, browser_id: str) -> dict[str, Any]:
        inst = self._require_instance(browser_id)
        inst.closing = True
        await inst._close()  # noqa: SLF001
        del self._instances[inst.browser_id]
        return {"browser_id": inst.browser_id, "status": "closed"}

    async def list_tabs(self, browser_id: str | None = None) -> list[dict]:
        if browser_id is not None:
            # See ADR-0042.
            async with self._targeted(browser_id) as inst:
                return [
                    {
                        "browser_id": inst.browser_id,
                        "tabs": await inst.list_tabs(),
                    }
                ]
        result = []
        for bid, inst in self._instances.items():
            result.append(
                {
                    "browser_id": bid,
                    "tabs": await inst.list_tabs(),
                }
            )
        return result

    async def open_tab(self, browser_id: str, url: str | None = None) -> dict[str, Any]:
        # See ADR-0022.
        async with self._targeted(browser_id) as inst:
            result = await inst.open_tab(url)
            if isinstance(result, dict):
                return {"browser_id": inst.browser_id, **result}
            return {"browser_id": inst.browser_id, "tab_id": result, "status": "opened"}

    async def page_snapshot(
        self,
        browser_id: str,
        tab_id: int,
        *,
        depth: int | None = None,
        boxes: bool = False,
        diff: bool = False,
    ) -> str:
        async with self._targeted(browser_id) as inst:
            return await inst.page_snapshot(tab_id, depth=depth, boxes=boxes, diff=diff)

    async def page_find(
        self,
        browser_id: str,
        tab_id: int,
        pattern: re.Pattern[str],
        *,
        boxes: bool = False,
    ) -> str:
        async with self._targeted(browser_id) as inst:
            return await inst.page_find(tab_id, pattern, boxes=boxes)

    async def close_tab(self, browser_id: str, tab_id: int) -> dict[str, Any]:
        # See ADR-0022, ADR-0040.
        async with self._targeted(browser_id) as inst:
            tab_dialog = next(
                (d for d in inst.open_dialogs() if d["tab_id"] == tab_id), None
            )
            closed_dialog = (
                {"type": tab_dialog["type"], "message": tab_dialog["message"]}
                if tab_dialog is not None
                else None
            )
            # See ADR-0040.
            last_tab = len(inst._tabs) == 1  # noqa: SLF001
            await inst.close_tab(tab_id)
            result: dict[str, Any] = {
                "browser_id": browser_id,
                "tab_id": tab_id,
                "status": "closed",
            }
            if closed_dialog is not None:
                result["closed_dialog"] = closed_dialog
            if last_tab:
                await self.close_instance(inst.browser_id)
                result["browser_closed"] = True
            return result

    async def navigate(
        self,
        browser_id: str,
        tab_id: int,
        url: str,
        *,
        timeout_ms: float = 30000.0,
        wait_until: str = "load",
    ) -> dict[str, Any]:
        # See ADR-0022.
        async with self._targeted(browser_id) as inst:
            return await inst.navigate(
                tab_id, url, timeout_ms=timeout_ms, wait_until=wait_until
            )

    async def eval_js(self, browser_id: str, tab_id: int, js_code: str) -> Any:
        async with self._targeted(browser_id) as inst:
            return await inst.eval_js(tab_id, js_code)

    async def wait_for(
        self, browser_id: str, tab_id: int, expression: str, *, timeout_ms: float
    ) -> Any:
        async with self._targeted(browser_id) as inst:
            return await inst.wait_for(tab_id, expression, timeout_ms=timeout_ms)

    async def screenshot(
        self,
        browser_id: str,
        tab_id: int,
        output_path: str | None = None,
        *,
        annotate: bool = False,
    ) -> tuple[str, str | None]:
        async with self._targeted(browser_id) as inst:
            return await inst.screenshot(tab_id, output_path, annotate=annotate)

    async def page_click(
        self, browser_id: str, tab_id: int, ref: str, *, timeout_ms: float
    ) -> dict[str, Any]:
        async with self._targeted(browser_id) as inst:
            return await inst.page_click(tab_id, ref, timeout_ms=timeout_ms)

    async def page_click_coords(
        self, browser_id: str, tab_id: int, *, x: float, y: float
    ) -> dict[str, Any]:
        async with self._targeted(browser_id) as inst:
            return await inst.page_click_coords(tab_id, x=x, y=y)

    async def page_hover_coords(
        self, browser_id: str, tab_id: int, *, x: float, y: float
    ) -> dict[str, Any]:
        async with self._targeted(browser_id) as inst:
            return await inst.page_hover_coords(tab_id, x=x, y=y)

    async def page_fill(
        self,
        browser_id: str,
        tab_id: int,
        ref: str,
        value: str,
        *,
        timeout_ms: float,
    ) -> dict[str, Any]:
        async with self._targeted(browser_id) as inst:
            return await inst.page_fill(tab_id, ref, value, timeout_ms=timeout_ms)

    async def page_hover(
        self, browser_id: str, tab_id: int, ref: str, *, timeout_ms: float
    ) -> dict[str, Any]:
        async with self._targeted(browser_id) as inst:
            return await inst.page_hover(tab_id, ref, timeout_ms=timeout_ms)

    async def page_upload(
        self,
        browser_id: str,
        tab_id: int,
        ref: str,
        files: list[str],
        *,
        timeout_ms: float,
    ) -> dict[str, Any]:
        async with self._targeted(browser_id) as inst:
            return await inst.page_upload(tab_id, ref, files, timeout_ms=timeout_ms)

    async def list_event_listeners(self, browser_id: str, tab_id: int) -> list[dict]:
        async with self._targeted(browser_id) as inst:
            return await inst.list_event_listeners(tab_id)

    async def cookies_list(
        self, browser_id: str, url: str | None = None
    ) -> list[dict[str, Any]]:
        async with self._targeted(browser_id) as inst:
            return await inst.cookies_list(url)

    async def cookies_clear(
        self,
        browser_id: str,
        name: str | None = None,
        domain: str | None = None,
        path: str | None = None,
    ) -> dict[str, Any]:
        async with self._targeted(browser_id) as inst:
            return await inst.cookies_clear(name, domain, path)

    async def cookies_set(
        self, browser_id: str, cookies: list[dict[str, Any]]
    ) -> dict[str, Any]:
        async with self._targeted(browser_id) as inst:
            return await inst.cookies_set(cookies)

    async def coverage_start(self, browser_id: str, tab_id: int) -> dict[str, Any]:
        async with self._targeted(browser_id) as inst:
            return await inst.coverage_start(tab_id)

    async def coverage_snapshot(self, browser_id: str, tab_id: int) -> dict[str, Any]:
        async with self._targeted(browser_id) as inst:
            return await inst.coverage_snapshot(tab_id)

    async def coverage_stop(self, browser_id: str, tab_id: int) -> dict[str, Any]:
        async with self._targeted(browser_id) as inst:
            return await inst.coverage_stop(tab_id)

    async def install_userscript(
        self, browser_id: str, name: str, source: str
    ) -> dict[str, Any]:
        async with self._targeted(browser_id) as inst:
            result = userscript_mod.install(inst.browser_id, name, source)
            result["extension_id"] = await inst._reload_userscript_extension()  # noqa: SLF001
            return result

    async def remove_userscript(self, browser_id: str, name: str) -> dict[str, Any]:
        async with self._targeted(browser_id) as inst:
            result = userscript_mod.remove(inst.browser_id, name)
            result["extension_id"] = await inst._reload_userscript_extension()  # noqa: SLF001
            return result

    def list_userscripts(self, browser_id: str) -> list[dict[str, Any]]:
        return userscript_mod.list_scripts(browser_id.lower())

    async def _wrap_add(
        self,
        browser_id: str,
        name: str,
        expr: str,
        install_fn,
    ) -> dict[str, Any]:
        async with self._targeted(browser_id) as inst:
            result = install_fn(inst.browser_id, name, expr)
            result["extension_id"] = await inst._reload_userscript_extension()  # noqa: SLF001
            return result

    async def wrap_calls_add(
        self, browser_id: str, name: str, expr: str
    ) -> dict[str, Any]:
        return await self._wrap_add(browser_id, name, expr, wrap_mod.install_call)

    async def wrap_access_add(
        self, browser_id: str, name: str, expr: str
    ) -> dict[str, Any]:
        return await self._wrap_add(browser_id, name, expr, wrap_mod.install_access)

    async def wrap_list(self, browser_id: str) -> list[dict[str, Any]]:
        # See ADR-0010.
        async with self._targeted(browser_id) as inst:
            return wrap_mod.list_wraps(inst.browser_id)

    async def wrap_remove(self, browser_id: str, name: str) -> dict[str, Any]:
        # See ADR-0010.
        async with self._targeted(browser_id) as inst:
            try:
                result = wrap_mod.remove(inst.browser_id, name)
            except ValueError as exc:
                raise BrowserOperationError(str(exc)) from exc
            result["extension_id"] = await inst._reload_userscript_extension()  # noqa: SLF001
            return result

    async def wrap_dump(
        self, browser_id: str, tab_id: int, name: str | None = None
    ) -> list[dict[str, Any]]:
        async with self._targeted(browser_id) as inst:
            return await inst.wrap_dump(tab_id, name)

    async def wrap_clear(self, browser_id: str, tab_id: int) -> dict[str, Any]:
        async with self._targeted(browser_id) as inst:
            return await inst.wrap_clear(tab_id)

    async def logpoint_add(
        self,
        browser_id: str,
        tab_id: int,
        url: str,
        line: int,
        col: int,
        expr: str,
    ) -> dict[str, Any]:
        async with self._targeted(browser_id) as inst:
            return await inst.logpoint_add(tab_id, url, line, col, expr)

    async def logpoint_list(self, browser_id: str, tab_id: int) -> list[dict[str, Any]]:
        async with self._targeted(browser_id) as inst:
            return await inst.logpoint_list(tab_id)

    async def logpoint_dump(self, browser_id: str, tab_id: int) -> list[dict[str, Any]]:
        async with self._targeted(browser_id) as inst:
            return await inst.logpoint_dump(tab_id)

    async def logpoint_clear(self, browser_id: str, tab_id: int) -> dict[str, Any]:
        async with self._targeted(browser_id) as inst:
            return await inst.logpoint_clear(tab_id)

    async def logpoint_remove(
        self, browser_id: str, tab_id: int, lp_id: str
    ) -> dict[str, Any]:
        async with self._targeted(browser_id) as inst:
            return await inst.logpoint_remove(tab_id, lp_id)

    async def handle_dialog(
        self,
        browser_id: str,
        tab_id: int,
        action: str,
        prompt_text: str | None = None,
    ) -> dict[str, Any]:
        async with self._targeted(browser_id) as inst:
            return await inst.handle_dialog(tab_id, action, prompt_text)
