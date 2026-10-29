"""Files between the app and its shellbox (shellbox/shellboxd.py, PUT and GET /file),
streamed both ways, never whole in memory.

While the shellbox is on, every file the user uploads (a chat attachment, a Documents
upload) is copied into its home at ~/uploads/<sha>/<name> (<sha> the first 12 characters
of its bytes' sha256, so the same bytes uploaded again land on the same file) right
after it is stored: in the background, so the upload never
waits for the box nor fails with it. A box that is down or full only costs a line in the
app log. A chat attachment keeps where its copy landed (attachments.box_path), and its
context names it. Files it makes or fetches are not copied.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path

import anyio
import httpx

from . import shellbox
from .settings import features

log = logging.getLogger("celine.box_files")

UPLOADS = "uploads"
CHUNK = 1 << 20
TRANSFER = 600          # seconds one chunk of a file may take
_tasks: set[asyncio.Task] = set()      # running copies, kept from the garbage collector


def safe_name(filename: str) -> str:
    """A plain file name: its base name only, no control characters, no leading dots."""
    name = re.sub(r"[\x00-\x1f\x7f]", "", (filename or "").replace("\\", "/")).split("/")[-1]
    return name.strip().lstrip(".").strip() or "file"


async def _chunks(src: Path):
    async with await anyio.open_file(src, "rb") as f:
        while chunk := await f.read(CHUNK):
            yield chunk


async def put(cfg, src: Path | bytes, rel: str, *, origin: str = "",
              transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """src (a file, or the bytes themselves) into its home at rel, tagged with `origin`
    when given (where others' text came from, for the guard). {"path", "bytes"};
    RuntimeError with the box's sentence."""
    size = len(src) if isinstance(src, bytes) else src.stat().st_size
    async with shellbox.client(cfg, TRANSFER, transport) as c:
        r = await c.put(shellbox.url(cfg) + "/file",
                        params={"path": rel,
                                **({"origin": origin} if origin else {})},
                        content=src if isinstance(src, bytes) else _chunks(src),
                        headers={"Content-Length": str(size)})
    if r.status_code != 200:
        try:
            said = r.json().get("error")
        except ValueError:
            said = None
        raise RuntimeError(said or f"The shellbox answered {r.status_code}.")
    return r.json()


async def stream(cfg, rel: str, *, transport: httpx.AsyncBaseTransport | None = None):
    """The agent's file at rel, as chunks of bytes. FileNotFoundError with the box's sentence on
    a 404, RuntimeError on another refusal, both before the first chunk."""
    async with shellbox.client(cfg, TRANSFER, transport) as c, \
            c.stream("GET", shellbox.url(cfg) + "/file", params={"path": rel}) as r:
        if r.status_code != 200:
            await r.aread()
            try:
                said = r.json().get("error")
            except ValueError:
                said = None
            if r.status_code == 404:
                raise FileNotFoundError(said or f"No file {rel} in the shellbox home.")
            raise RuntimeError(said or f"The shellbox answered {r.status_code}.")
        async for chunk in r.aiter_bytes(CHUNK):
            yield chunk


async def copy_upload(cfg, src: Path, filename: str, store=None, aid: str | None = None,
                      transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    """The user's upload into ~/uploads/; with aid, the attachment keeps where it landed.
    The agent's box's path, or None when it did not get there (said in the log)."""
    if not shellbox.url(cfg):
        return None
    try:
        sha = await anyio.to_thread.run_sync(_sha, src)
        res = await put(cfg, src, f"{UPLOADS}/{sha[:12]}/{safe_name(filename)}",
                        origin=f"upload:{filename}", transport=transport)
    except (httpx.HTTPError, RuntimeError, OSError) as e:
        log.warning("%s did not reach the box: %s", filename,
                    e if isinstance(e, RuntimeError) else "the shellbox is not reachable.")
        return None
    path = str(res["path"])
    if store is not None and aid is not None:
        from .attachments import set_box_path
        await set_box_path(store, aid, path)
    return path


def _sha(src: Path) -> str:
    with src.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def copy_later(cfg, src: Path, filename: str, store=None, aid: str | None = None) -> None:
    """copy_upload in the background, while the shellbox is on: the upload's answer never
    waits for it."""
    if not features.on(cfg, "shellbox") or not shellbox.url(cfg):
        return
    t = asyncio.get_running_loop().create_task(copy_upload(cfg, src, filename, store, aid))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
