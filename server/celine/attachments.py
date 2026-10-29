"""Files attached to messages. Content-addressed under var/blobs, two-level fanout.

Images and the model that answers: the conversation's model always answers, and an
image adapts to it. `render` leaves each image as an `IMAGE` part; the queued backend
(backends/llm.py) hands every request to `Images.wire` once it knows which model
answers, so a fallback in the chat list gets what it can take:
- a model that sees images (the `vision` capability) gets the image itself, at most
  WIRE_EDGE px on its long edge (`for_model`);
- any other model gets the image's photo description as text, marked as one. The
  Photo descriptions role writes it the first time it is needed (kb/media.py, thinking
  off), and it is kept with the attachment, so later requests and other models reuse
  it. The message shows a note: "<model> can't see images: <photo model> described it."
- when no model of Photo descriptions can answer, the model gets a line that an image
  is there and it cannot see it. The composer blocks such a send before it happens
  (`support`, conversations/api.py).

An image or a track a batch makes (comfyui/batches.py) is stored the same way and
belongs to its batch, and in its batch also to the message that holds its call. That
message goes to a model as its tool calls only, so a generated image never reaches a
model. A batch the user started has no message: its files are the batch's alone. A
picture a tool keeps for the user, like a browser shot (skills/browser.py), belongs to
the call alone: no message holds it, so only the call's row shows it, and its image
list and edits never see it.

What stays (`gc`, at each start; KEEP is 30 days). A file stays while an attachment of a
message, a tool call or a batch, or its portrait, points at it. A draft (an upload not
sent yet) keeps its file until it is sent, discarded or KEEP old. A file nothing points
at goes once it is KEEP old, a half-written upload once untouched for KEEP. A picture's
smaller copies (`resized`) go with its file. A file and
its rows are written and dropped in one writer job, so gc never sees one without the other.
"""
from __future__ import annotations

import base64
import functools
import json
import hashlib
import logging
import mimetypes
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import anyio
from PIL import ExifTags, Image, ImageOps

from .conversations import store as conversations
from .core.ids import iso, new_id, now
from .errors import TooLarge
from . import shellbox
from .core.event_types import ATTACHMENT_DESCRIBED, MESSAGE_ATTACHMENTS

log = logging.getLogger("celine.attachments")

IMAGE = "attachment_image"     # an image part until the answering model is known
KEEP = timedelta(days=30)      # a draft, a file nothing points at, a half-written upload

_TEXT_TYPES = {"application/json", "application/xml", "application/x-yaml",
               "application/yaml", "application/toml", "application/javascript",
               "application/x-sh", "application/sql"}


def blob_path(cfg, sha: str) -> Path:
    return cfg.blobs_dir / sha[:2] / sha[2:4] / sha


async def save(cfg, store, upload) -> dict:
    """Stream a FastAPI UploadFile to disk, hashing as it goes."""
    async def chunks():
        while chunk := await upload.read(1 << 20):
            yield chunk
    return await save_stream(cfg, store, chunks(), upload.filename or "file",
                             upload.content_type,
                             limit=int(cfg.r("max_attachment_bytes")))


async def save_stream(cfg, store, chunks, filename: str, media_type: str | None, *,
                      limit: int | None = None, tool_call_id: str | None = None,
                      in_message: bool = True) -> dict:
    """An attachment from an async iterable of bytes, written and hashed as it comes;
    with tool_call_id, a file that call made (as save_generated)."""
    tmp_dir = cfg.blobs_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / new_id()
    h = hashlib.sha256()
    total = 0
    head = b""
    try:
        with tmp.open("wb") as f:
            async for chunk in chunks:
                total += len(chunk)
                if limit is not None and total > limit:
                    raise TooLarge(filename or "This file", total, limit)
                h.update(chunk)
                f.write(chunk)
                if len(head) < 4096:
                    head += chunk[:4096 - len(head)]
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return await _finish(cfg, store, tmp, h.hexdigest(), total, head, filename, media_type,
                         tool_call_id=tool_call_id, in_message=in_message)


async def save_generated(cfg, store, data: bytes, filename: str, media_type: str,
                         tool_call_id: str | None, *, in_message: bool = True,
                         batch_id: str | None = None,
                         described: tuple[str, str] | None = None) -> dict:
    """A file a tool or a batch made, attached to the message that holds the tool call;
    not `in_message`, to the call alone; with `batch_id`, also to its batch (see the
    module docstring). `described`: (its photo description, who wrote it), kept on it."""
    tmp_dir = cfg.blobs_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / new_id()
    tmp.write_bytes(data)
    return await _finish(cfg, store, tmp, hashlib.sha256(data).hexdigest(), len(data),
                         data[:4096], filename, media_type, tool_call_id=tool_call_id,
                         in_message=in_message, batch_id=batch_id, described=described)


async def _finish(cfg, store, tmp: Path, sha: str, size: int, head: bytes,
                  filename: str, content_type: str | None, **where) -> dict:
    """The rows, then the file into place, in one writer job (the module docstring)."""
    if content_type == "application/octet-stream":     # "some bytes": its name says more
        content_type = None
    media_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if media_type == "application/octet-stream" and _looks_like_text(head):
        media_type = "text/plain"
    kind = _kind(media_type)
    final = blob_path(cfg, sha)
    pixels = await anyio.to_thread.run_sync(_pixels, tmp) if kind == "image" else None

    def fn(c: sqlite3.Connection) -> dict:
        row = _insert(c, sha, size, media_type, filename, kind, store=store, pixels=pixels,
                      **where)
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp.replace(final)
        return row

    try:
        row = await store.write(fn)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    log.info("attachment saved", extra={"event": "attachment.saved", "kind": kind,
                                        "bytes": size})
    return row


def _looks_like_text(head: bytes) -> bool:
    if not head or b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _kind(media_type: str) -> str:
    if media_type.startswith("image/"):
        return "image"
    if media_type.startswith("audio/"):
        return "audio"
    if media_type.startswith("text/") or media_type in _TEXT_TYPES:
        return "text"
    return "binary"


def render(cfg, history: list[dict]) -> list[dict]:
    """Wire messages with attachments turned into content parts or notes. An image
    becomes an `IMAGE` part; `Images.wire` turns it into what the answering model
    takes."""
    inline_limit = int(cfg.r("inline_text_max_bytes"))
    out = []
    for m in history:
        atts = m.get("attachments")
        if not atts:
            out.append({"role": m["role"], "content": m["content"]})
            continue
        parts: list[dict] = []
        notes: list[str] = []
        for a in atts:
            path = blob_path(cfg, a["sha256"])
            box = a.get("box_path")
            where = f", in your box at {shellbox.home_short(box)}" if box else ""
            if a["kind"] == "image":
                if path.exists():
                    parts.append({"type": IMAGE, "attachment": {
                        **{k: a[k] for k in ("id", "filename", "sha256", "media_type")},
                        "size": image_size_of(path)}})
                    if box:
                        notes.append(f"[image {a['filename']}{where}]")
                else:
                    notes.append(f"[image {a['filename']}: the file is no longer stored]")
            elif a["kind"] == "text" and a["bytes"] <= inline_limit and path.exists():
                notes.append(f"[attachment {a['filename']}{where}]\n"
                             f"{path.read_text(errors='replace')}\n[end of {a['filename']}]")
            else:
                notes.append(f"[attachment {a['filename']}, {size(a['bytes'])}, "
                             f"{a['media_type']}{where or ', not inlined'}]")
        text = "\n\n".join(([m["content"]] if m["content"] else []) + notes)
        if parts:
            parts.insert(0, {"type": "text", "text": text})
            out.append({"role": m["role"], "content": parts})
        else:
            out.append({"role": m["role"], "content": text})
    return out


def label(settings, ref: str | None) -> str:
    """A model's name for people: its label in Settings, else its name."""
    if not ref or settings is None:
        return ref or ""
    return (settings.model(ref) or {}).get("label") or settings.split(ref)[1]


def sees_images(settings, ref: str | None) -> bool:
    return settings is not None and bool(ref) and \
        "vision" in ((settings.model(ref) or {}).get("caps") or [])


async def turn_sees_images(cfg, ctx) -> bool:
    """Whether the model answering this tool call's turn takes images: the turn's
    backend, else the conversation's first model."""
    settings = cfg.settings
    if settings is None:
        return False
    row = await ctx.store.read(lambda c: c.execute(
        "SELECT backend_id FROM turns WHERE id=?", (ctx.turn_id,)).fetchone())
    ref = row["backend_id"] if row else None
    if not ref:
        chain = settings.chat_chain(ctx.conversation_id)
        ref = chain[0] if chain else None
    return sees_images(settings, ref)


async def describer(llm) -> str | None:
    """The model of the Photo descriptions role that would answer now, or None."""
    if llm is None:
        return None
    ordered = await llm.router.order("vision")
    return label(llm.router.s, ordered.refs[0]) if ordered.refs else None


async def support(cfg, llm, ref: str | None, fixed=None) -> dict[str, Any]:
    """Whether a message with images can go to this chat model now: {model, sees,
    describer}. `sees`: the model takes images itself. Else `describer` names the
    Photo descriptions model that writes the descriptions, or is None when none can
    answer (then a message with images is refused). `fixed`: a test backend."""
    s = cfg.settings
    if fixed is not None:
        name, sees = fixed.model, bool(getattr(fixed, "supports_images", False))
    else:
        name, sees = label(s, ref), sees_images(s, ref)
    return {"model": name, "sees": sees,
            "describer": None if sees else await describer(llm)}


async def after_switch(store, cfg, llm, cid: str, before: str | None) -> None:
    """The conversation's model changed from `before`: when the new one cannot see
    images and the conversation holds images, one line in the transcript says how the
    earlier images reach it (store.note_images)."""
    s = cfg.settings
    chain = s.chat_chain(cid) if s is not None else []
    model = chain[0] if chain else None
    if model is None or model == before or sees_images(s, model) \
            or not await has_images(store, cid):
        return
    await conversations.note_images(store, cid, label(s, model), await describer(llm))


class Images:
    """One turn's images for the model that answers a request (see the module
    docstring). `lane`: the queue lane of the turn's description requests."""

    def __init__(self, cfg, store, llm, lane: str):
        self.cfg, self.store, self.llm, self.lane = cfg, store, llm, lane

    async def wire(self, messages: list[dict], backend, model: str) -> list[dict]:
        """`messages` as `backend` (named `model` for people) can take them. Pixels go
        only for the last PIXELS_KEPT messages that carry images; older images go as
        their description, like for a model that cannot see (an image went on the wire
        at full size on every request of every later turn: 9 MB of base64 per request,
        measured)."""
        if not any(_has_image(m) for m in messages):
            return messages
        sees = bool(getattr(backend, "supports_images", False))
        with_images = [i for i, m in enumerate(messages) if _has_image(m)]
        recent = set(with_images[-PIXELS_KEPT:])
        out = []
        for i, m in enumerate(messages):
            if not _has_image(m):
                out.append(m)
            elif sees and i in recent:
                out.append({**m, "content": [await self._image(p) if p.get("type") == IMAGE
                                             else p for p in m["content"]]})
            else:
                texts = [await self._described(p["attachment"], model)
                         if p.get("type") == IMAGE else p.get("text") or ""
                         for p in m["content"]]
                out.append({**m, "content": "\n\n".join(t for t in texts if t)})
        return out

    async def _image(self, part: dict) -> dict:
        """The image at most WIRE_EDGE px on its long edge (the copy `resized` keeps)."""
        a = part["attachment"]
        path, media = await anyio.to_thread.run_sync(for_model, self.cfg, a["sha256"],
                                                     a["media_type"])
        b64 = base64.b64encode(path.read_bytes()).decode()
        return {"type": "image_url", "image_url": {"url": f"data:{media};base64,{b64}"}}

    async def _described(self, a: dict, model: str) -> str:
        """The image's photo description as a line for `model`, written now when the
        attachment has none yet. An attachment belongs to one conversation, which runs
        one turn at a time, so one image is never described twice at once."""
        from .kb.media import CaptionError, Engine, Waiting
        row = await get(self.store, a["id"]) or {}
        if row.get("description"):
            return _line(a["filename"], row["described_by"], row["description"])
        try:
            data = blob_path(self.cfg, a["sha256"]).read_bytes()
            out = await Engine(self.cfg, llm=self.llm).describe(data, a["media_type"],
                                                                lane=self.lane)
        except (Waiting, CaptionError, OSError) as e:
            log.info("no photo description", extra={
                "event": "attachment.not_described", "error": str(e)})
            return (f"[Image {a['filename']}: {model} cannot see images, and no photo "
                    f"description is available.]")
        text = out["caption"]
        if out.get("visible_text"):
            text += f" Text in the image: \"{out['visible_text']}\""
        by = label(self.cfg.settings, out.get("endpoint")) or out["model"]
        await describe(self.store, a["id"], text, by, model)
        return _line(a["filename"], by, text)


PIXELS_KEPT = 2       # messages whose images go as pixels; older ones as descriptions


def image_size(data: bytes) -> tuple[int, int] | None:
    """Width and height of a PNG or JPEG from its header; None for other formats."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data) and data[i] == 0xFF:
            marker, length = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return (int.from_bytes(data[i + 7:i + 9], "big"),
                        int.from_bytes(data[i + 5:i + 7], "big"))
            i += 2 + length
    return None


def image_size_of(path) -> tuple[int, int] | None:
    """(width, height) from the file's header (PNG, JPEG), once per blob: the token
    estimate counts an image by its size (turns/tokens.py)."""
    try:
        return _image_size_of(str(path))
    except OSError:          # not cached: the file may come back
        return None


@functools.lru_cache(maxsize=1024)
def _image_size_of(path: str) -> tuple[int, int] | None:
    return image_size(Path(path).read_bytes())


_TURNED = (5, 6, 7, 8)      # EXIF orientations that turn a picture by a quarter
Image.MAX_IMAGE_PIXELS = 50_000_000   # Pillow refuses a picture past twice this (a bomb)


def _pixels(path: Path) -> tuple[int, int] | None:
    """(width, height) of a picture Pillow reads, as it is shown (its EXIF turn
    applied), from its header; None for anything else."""
    try:
        with Image.open(path) as im:
            w, h = im.size
            return (h, w) if im.getexif().get(ExifTags.Base.Orientation) in _TURNED else (w, h)
    except Exception:            # a broken file: Pillow raises many kinds
        return None


WIDTHS = (320, 640, 1280)   # the widths a picture is shown at: few, so the copies stay few


def snap(want: int) -> int:
    """The first of WIDTHS at least `want` wide, else the widest."""
    return next((w for w in WIDTHS if w >= want), WIDTHS[-1])


def resized(cfg, sha: str, width: int) -> Path:
    """The picture `width` px wide as WebP, made once and kept beside its blob as
    `<sha>.w<width>.webp` (gc drops it with the blob). The blob itself when it is not
    wider, moves (an animation) or is not a picture Pillow reads. The blob never
    changes. Blocking: a thread's work."""
    src = blob_path(cfg, sha)
    out = src.with_name(f"{sha}.w{width}.webp")
    if out.exists():
        return out
    part = out.with_name(f"{out.name}.{new_id()}.part")
    try:
        with Image.open(src) as im:
            if getattr(im, "is_animated", False):
                return src
            im = ImageOps.exif_transpose(im)
            if im.width <= width:
                return src
            im.thumbnail((width, im.height))
            alpha = "A" in im.getbands() or "transparency" in im.info
            im.convert("RGBA" if alpha else "RGB").save(part, "WEBP", quality=80)
    except Exception:            # a broken file: Pillow raises many kinds
        part.unlink(missing_ok=True)
        return src
    part.replace(out)
    return out


WIRE_EDGE = 1568     # the longest edge of a picture sent to a model; encoders scale a
                     # larger one down anyway


def for_model(cfg, sha: str, media_type: str) -> tuple[Path, str]:
    """(the file, its media type) a model gets for a picture: at most WIRE_EDGE px on its
    long edge. Blocking: a thread's work."""
    pixels = _pixels(blob_path(cfg, sha))
    if pixels is None or max(pixels) <= WIRE_EDGE:
        return blob_path(cfg, sha), media_type
    w, h = pixels
    path = resized(cfg, sha, w * WIRE_EDGE // max(w, h))
    return path, "image/webp" if path.suffix == ".webp" else media_type


def size(b: int) -> str:
    return f"{b / 1048576:.1f} MB" if b >= 1048576 else f"{b} bytes"


def _has_image(m: dict) -> bool:
    c = m.get("content")
    return isinstance(c, list) and any(isinstance(p, dict) and p.get("type") == IMAGE
                                       for p in c)


def _line(filename: str, by: str, text: str) -> str:
    return f"[Image {filename}, described by {by}: {text}]"


async def gc(cfg, store, at: datetime | None = None) -> int:
    """What the module docstring says goes, as of `at` (now). -> how many files went."""
    old = (at or datetime.now(timezone.utc)) - KEEP

    def fn(c: sqlite3.Connection) -> list[str]:
        c.execute(f"DELETE FROM attachments WHERE {_UNBOUND} AND batch_id IS NULL AND "
                  f"created_at < ?", (iso(old),))
        shas = [r["sha256"] for r in c.execute(
            "SELECT sha256 FROM blobs WHERE created_at < ? AND sha256 NOT IN "
            "(SELECT sha256 FROM attachments) AND sha256 NOT IN "
            "(SELECT portrait FROM her WHERE portrait IS NOT NULL)", (iso(old),))]
        for sha in shas:
            c.execute("DELETE FROM blobs WHERE sha256=?", (sha,))
            path = blob_path(cfg, sha)
            path.unlink(missing_ok=True)
            for copy in path.parent.glob(f"{sha}.w*.webp"):     # resized()
                copy.unlink(missing_ok=True)
        return shas

    orphans = await store.write(fn)
    tmp_dir = cfg.blobs_dir / "tmp"
    if tmp_dir.exists():
        for p in tmp_dir.iterdir():
            if p.stat().st_mtime < old.timestamp():
                p.unlink(missing_ok=True)
    for p in cfg.blobs_dir.glob("*/*/*.part"):         # a copy `resized` never finished
        if p.stat().st_mtime < old.timestamp():
            p.unlink(missing_ok=True)
    if orphans:
        log.info("blobs swept", extra={"event": "attachment.gc", "count": len(orphans)})
    return len(orphans)


# ---- the rows: attachments and the blobs they point at ----------------------------

# one field of the size of a picture it made, from its batch item
MADE = ("SELECT je.value ->> '$.attachment.{}' FROM media_batches mb, json_each(mb.items) je "
        "WHERE mb.id = a.batch_id AND je.value ->> '$.attachment.id' = a.id")


def of_message(c: sqlite3.Connection, message_id: str) -> list[dict]:
    """A message's attachments, inside a read or write (conversations/tree.py). A
    file a batch made has its batch_id (its gallery shows it). A picture has its size,
    from its blob or the batch item that made it, so its box has its shape before it
    loads."""
    return [dict(r) for r in c.execute(
        "SELECT a.id, a.filename, a.kind, a.sha256, b.bytes, b.media_type, "
        "a.description, a.described_by, a.described_for, a.box_path, a.batch_id, "
        f"coalesce(b.width, ({MADE.format('width')})) AS width, "
        f"coalesce(b.height, ({MADE.format('height')})) AS height "
        "FROM attachments a JOIN blobs b ON b.sha256 = a.sha256 "
        "WHERE a.message_id=?1 OR a.id = (SELECT json_extract(meta, '$.attachment') "
        "FROM messages WHERE id=?1 AND json_extract(meta, '$.kind') = 'image') "
        "ORDER BY a.created_at, a.id", (message_id,))]


def with_files(c: sqlite3.Connection, call_id: str, display: dict | None) -> dict | None:
    """The call's display with one `files` block (`where: under`): every file kept with
    the call alone (an MCP image, a shell look, a document image), so its row shows it
    whatever the tool. A file its display already names (a browser shot in its steps)
    stays out; files on its message show there (shell__send)."""
    named = json.dumps({k: v for k, v in (display or {}).items() if k != "blocks"})
    files = [dict(r) for r in c.execute(
        "SELECT a.id, a.filename, a.kind, a.sha256, b.bytes, b.media_type, b.width, "
        "b.height FROM attachments a JOIN blobs b ON b.sha256 = a.sha256 "
        "WHERE a.tool_call_id=? ORDER BY a.rowid", (call_id,))
        if f'"{r["id"]}"' not in named]
    blocks = [b for b in (display or {}).get("blocks") or () if b.get("type") != "files"]
    if files:
        blocks.append({"type": "files", "where": "under", "files": files})
    out = {k: v for k, v in (display or {}).items() if k != "blocks"}
    if blocks:
        out["blocks"] = blocks
    return out or None


async def add(store, sha: str, size: int, media_type: str, filename: str, kind: str, *,
              tool_call_id: str | None = None, in_message: bool = True,
              batch_id: str | None = None) -> dict:
    """A draft that the next message you send takes along; with `tool_call_id`, a
    file that call made: an attachment of the message that holds the call (an image
    Celine made), or, not `in_message`, of the call alone (a browser shot); with
    `batch_id`, a file that batch made. KeyError: no such tool call. The rows only:
    the file is the caller's."""
    return await store.write(lambda c: _insert(
        c, sha, size, media_type, filename, kind, store=store, tool_call_id=tool_call_id,
        in_message=in_message, batch_id=batch_id))


def _insert(c: sqlite3.Connection, sha: str, size: int, media_type: str, filename: str,
            kind: str, *, store, tool_call_id: str | None = None, in_message: bool = True,
            batch_id: str | None = None, described: tuple[str, str] | None = None,
            pixels: tuple[int, int] | None = None) -> dict:
    """The rows; a file that joins a message already there tells its conversation
    (event `message.attachments`, the message's whole list). `pixels`: an image's
    (width, height), kept on its blob."""
    aid, ts = new_id(), now()
    mid = call = None
    if tool_call_id is not None:
        r = c.execute("SELECT t.message_id, m.conversation_id FROM tool_calls t "
                      "JOIN messages m ON m.id = t.message_id WHERE t.id=?",
                      (tool_call_id,)).fetchone()
        if r is None:
            raise KeyError(tool_call_id)
        if in_message:
            mid = r["message_id"]
        else:
            call = tool_call_id
    text, by = described or (None, None)
    w, h = pixels or (None, None)
    c.execute("INSERT INTO blobs(sha256,bytes,media_type,width,height,created_at) "
              "VALUES(?,?,?,?,?,?) ON CONFLICT(sha256) DO UPDATE SET "
              "width=coalesce(width, excluded.width), height=coalesce(height, excluded.height)",
              (sha, size, media_type, w, h, ts))
    c.execute("INSERT INTO attachments(id,message_id,tool_call_id,batch_id,sha256,"
              "filename,kind,state,created_at,description,described_by,described_for) "
              "VALUES(?,?,?,?,?,?,?,'ready',?,?,?,?)",
              (aid, mid, call, batch_id, sha, filename, kind, ts, text, by,
               "" if described else None))
    if mid is not None:
        store.event(c, r["conversation_id"], MESSAGE_ATTACHMENTS,
                    {"message_id": mid, "attachments": of_message(c, mid)})
    return {"id": aid, "filename": filename, "kind": kind, "sha256": sha,
            "bytes": size, "media_type": media_type}


async def get(store, aid: str) -> dict | None:
    def fn(c: sqlite3.Connection) -> dict | None:
        r = c.execute(
            "SELECT a.id, a.message_id, a.filename, a.kind, a.sha256, b.bytes, "
            "b.media_type, a.description, a.described_by, a.described_for "
            "FROM attachments a JOIN blobs b ON b.sha256 = a.sha256 "
            "WHERE a.id=?", (aid,)).fetchone()
        return dict(r) if r else None

    return await store.read(fn)


async def describe(store, aid: str, text: str, by: str, for_model: str) -> None:
    """Keeps an image's photo description with its attachment (written once) and
    tells the conversation (event `attachment.described`)."""
    def fn(c: sqlite3.Connection) -> None:
        r = c.execute("SELECT a.message_id, m.conversation_id FROM attachments a "
                      "JOIN messages m ON m.id = a.message_id "
                      "WHERE a.id=? AND a.description IS NULL", (aid,)).fetchone()
        if r is None:
            return
        c.execute("UPDATE attachments SET description=?, described_by=?, "
                  "described_for=? WHERE id=?", (text, by, for_model, aid))
        store.event(c, r["conversation_id"], ATTACHMENT_DESCRIBED, {
            "message_id": r["message_id"], "attachment_id": aid,
            "description": text, "described_by": by, "described_for": for_model})

    await store.write(fn)


async def set_box_path(store, aid: str, path: str) -> None:
    """Where the attachment's copy landed in its box (box_files.py)."""
    await store.write(lambda c: c.execute(
        "UPDATE attachments SET box_path=? WHERE id=?", (path, aid)))


async def images(store, conversation_id: str | None = None,
                 aid: str | None = None) -> list[dict]:
    """Sent image attachments with the role of their message and its conversation,
    newest first: those of one conversation, or those whose id starts with `aid`."""
    def fn(c: sqlite3.Connection) -> list[dict]:
        where, args = (("substr(a.id, 1, ?)=?", (len(aid), aid)) if aid
                       else ("m.conversation_id=?", (conversation_id,)))
        out = []
        for r in c.execute(
                "SELECT a.id, a.filename, a.sha256, b.media_type, m.role, "
                "m.conversation_id, a.created_at, "
                # the batch item that made it: prompt, preset, seed, size, source
                "(SELECT je.value FROM media_batches mb, json_each(mb.items) je "
                " WHERE mb.id = a.batch_id AND je.value ->> '$.attachment.id' = a.id) "
                "AS made "
                "FROM attachments a JOIN blobs b ON b.sha256 = a.sha256 "
                "JOIN messages m ON m.id = a.message_id "
                f"WHERE a.kind='image' AND {where} "
                "ORDER BY m.created_at DESC, a.created_at DESC", args):
            d = dict(r)
            d["made"] = json.loads(d["made"]) if d["made"] else None
            out.append(d)
        return out

    return await store.read(fn)


SHORT = 8           # an image's short id: the first characters of its attachment id


async def her_image(store, ref: str, cid: str | None, user: str) -> dict | str:
    """An image it may use by its short id (or a longer start of its id): one it made
    (an attachment of its own message, in any conversation) or one the user attached to
    conversation `cid`. -> its attachment row, or the sentence that says why not."""
    wanted = ref
    if not re.fullmatch(r"[0-9a-f]{4,32}", wanted):
        return (f"{wanted} is not an image's short id: use the {SHORT} characters from "
                f"its result or list_images, like f5ef648c.")
    rows = await images(store, aid=wanted)
    if len({r["id"] for r in rows}) > 1:
        return (f"Several images start with {wanted}: use all {SHORT} characters of its "
                f"short id.")
    row = next((r for r in rows if r["role"] == "assistant" or (
        r["role"] == "user" and r["conversation_id"] == cid)), None)
    if row is None:
        return (f"There is no image {wanted} that you may use: only an image you made, or "
                f"one {user} attached to this conversation.")
    return row


async def has_images(store, conversation_id: str) -> bool:
    def fn(c: sqlite3.Connection) -> bool:
        return c.execute(
            "SELECT 1 FROM attachments a JOIN messages m ON m.id = a.message_id "
            "WHERE m.conversation_id=? AND m.role='user' AND a.kind='image' LIMIT 1",
            (conversation_id,)).fetchone() is not None

    return await store.read(fn)


_UNBOUND = "message_id IS NULL AND tool_call_id IS NULL"


async def delete_draft(store, aid: str) -> bool:
    def fn(c: sqlite3.Connection) -> bool:
        return c.execute(f"DELETE FROM attachments WHERE id=? AND {_UNBOUND}",
                         (aid,)).rowcount == 1

    return await store.write(fn)

