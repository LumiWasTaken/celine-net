"""Media files and image captions.

Files live content-addressed under `<data>/kb/<kb>.media/<sha256>` and a message
lists its files in `media.files: [{"name", "size", "content_type", "sha256"}]`
(Discord attachments, WhatsApp files matched from the phone's folder or downloaded).

The caption queue: a background task per knowledge base picks image files without a
caption, newest message first, and asks a vision model for a short factual English
caption plus any visible text. Captions are stored per file (`media_captions`, by
sha256) and become searchable: kb/instant.py and kb/passages.py read them through
kb/media_text.py, and a new caption marks its messages dirty in both indexes
(`search_dirty`, `passage_dirty`).

The model is the Photo descriptions task in Settings, Models: each image is one
request through the app's LLM queue at background priority (a user's turn always goes
first), on the first model of the task that can answer (backends/router.py). When
none can, the run ends and the job waits for its next run.

Config: config/kb.toml [media]; everything has a default. Whether the queue runs on its
own is `image_captions_auto` (Settings, General); off, it does not run.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import prompts
from ..core.ids import now_ms
from ..settings import features, general as general_mod
from .db import in_batches
from .importers import ImportContext

log = logging.getLogger("celine.kb.media")

PROMPT_VER = 1
# the prompt: image.md beside this module (PROMPT_VER counts its versions)

IMAGE_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
             ".webp": "image/webp"}
SUPPORTED = {"image/jpeg", "image/png", "image/webp"}


def media_dir(kb) -> Path:
    return kb.path.parent / f"{kb.id}.media"


def sniff(data: bytes) -> str | None:
    """The image type from its first bytes; None when it is not one we send."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


# ---- settings ------------------------------------------------------------------------

@dataclass
class Settings:
    types: tuple[str, ...] = ("photo", "sticker")
    batch: int = 20
    rate_per_minute: float = 0.0             # 0: no limit
    max_attempts: int = 3
    max_mb: float = 15.0
    poll_minutes: float = 10.0
    msgstore: str = ""
    wa_download: bool = False
    wa_media_retry: bool = False
    wa_retry_per_run: int = 20                 # requests to the phone per run, at most
    wa_retry_interval_seconds: float = 3.0
    wa_retry_wait_seconds: float = 120.0

    @classmethod
    def parse(cls, raw: dict[str, Any] | None) -> "Settings":
        raw = raw or {}
        return cls(types=tuple(str(x) for x in raw.get("types", ("photo", "sticker"))),
                   batch=max(1, int(raw.get("batch", 20))),
                   rate_per_minute=float(raw.get("rate_per_minute", 0)),
                   max_attempts=max(1, int(raw.get("max_attempts", 3))),
                   max_mb=float(raw.get("max_mb", 15)),
                   poll_minutes=float(raw.get("poll_minutes", 10)),
                   msgstore=str(raw.get("msgstore", "")),
                   wa_download=bool(raw.get("wa_download", False)),
                   wa_media_retry=bool(raw.get("wa_media_retry", False)),
                   wa_retry_per_run=max(0, int(raw.get("wa_retry_per_run", 20))),
                   wa_retry_interval_seconds=float(raw.get("wa_retry_interval_seconds", 3)),
                   wa_retry_wait_seconds=float(raw.get("wa_retry_wait_seconds", 120)))


# ---- what to caption --------------------------------------------------------------------

@dataclass
class Job:
    sha256: str
    name: str | None
    content_type: str | None
    ts: int
    messages: list[tuple[int, int]]          # (message id, chat id)


def _is_image(f: dict[str, Any]) -> bool:
    ct = str(f.get("content_type") or "").split(";")[0].strip().lower()
    if ct:
        return ct in SUPPORTED
    return Path(str(f.get("name") or "")).suffix.lower() in IMAGE_EXT


def _caption_state(conn: sqlite3.Connection) -> dict[str, tuple[str, int]]:
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT sha256, state, attempts FROM media_captions")}


def image_files(conn: sqlite3.Connection, types: tuple[str, ...]) -> dict[str, Job]:
    """Every image file on record, by sha256, newest message first."""
    out: dict[str, Job] = {}
    for r in conn.execute("SELECT id, chat_id, ts, media FROM messages "
                          "WHERE kind = 'media' AND media LIKE '%\"sha256\"%' "
                          "ORDER BY ts DESC, id DESC"):
        try:
            m = json.loads(r["media"])
        except ValueError:
            continue
        if not isinstance(m, dict) or (types and m.get("type") not in types):
            continue
        for f in m.get("files") or []:
            if not isinstance(f, dict) or not f.get("sha256") or not _is_image(f):
                continue
            j = out.get(f["sha256"])
            if j is None:
                out[f["sha256"]] = Job(f["sha256"], f.get("name"), f.get("content_type"),
                                       r["ts"], [(r["id"], r["chat_id"])])
            else:
                j.messages.append((r["id"], r["chat_id"]))
    return out


def pick(conn: sqlite3.Connection, s: Settings, limit: int,
         exclude: set[str] | None = None) -> list[Job]:
    """Up to `limit` image files without a caption, newest first. Failed ones come
    back until they failed `max_attempts` times."""
    state = _caption_state(conn)
    exclude = exclude or set()
    out = []
    for sha, job in image_files(conn, s.types).items():
        if sha in exclude:
            continue
        st = state.get(sha)
        if st is not None and (st[0] in ("done", "skipped") or st[1] >= s.max_attempts):
            continue
        out.append(job)
        if len(out) >= limit:
            break
    return out


def stats(kb, s: Settings) -> dict[str, Any]:
    def run(conn: sqlite3.Connection) -> dict[str, Any]:
        files = image_files(conn, s.types)
        state = _caption_state(conn)
        d = media_dir(kb)
        on_disk = sum(1 for sha in files if (d / sha).is_file())
        counts = {"done": 0, "failed": 0, "skipped": 0}
        for sha in files:
            st = state.get(sha)
            if st is not None and st[0] in counts:
                counts[st[0]] += 1
        waiting = sum(1 for sha in files if sha not in state
                      or (state[sha][0] == "failed" and state[sha][1] < s.max_attempts))
        media = {r[0]: r[1] for r in conn.execute(
            "SELECT json_extract(media, '$.type'), COUNT(*) FROM messages "
            "WHERE kind = 'media' GROUP BY 1")}
        with_file = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE kind = 'media' "
            "AND media LIKE '%\"sha256\"%'").fetchone()[0]
        models = {r[0]: r[1] for r in conn.execute(
            "SELECT model, COUNT(*) FROM media_captions WHERE state = 'done' GROUP BY 1")}
        return {"kb": kb.id, "media_messages": media, "messages_with_files": with_file,
                "image_files": len(files), "image_files_on_disk": on_disk,
                "captions": counts, "waiting": waiting, "models": models}
    return kb.file.read_sync(run)


def caption_of(kb, shas: list[str]) -> dict[str, dict[str, Any]]:
    def run(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        out = {}
        for part in in_batches(shas):
            for r in conn.execute(
                    "SELECT * FROM media_captions WHERE sha256 IN "
                    f"({','.join('?' * len(part))})", part):
                out[r["sha256"]] = {k: r[k] for k in r.keys()}
        return out
    return kb.file.read_sync(run)


# ---- storing -----------------------------------------------------------------------------

def store(conn: sqlite3.Connection, job: Job, *, state: str, caption: str | None = None,
          visible_text: str | None = None, model: str | None = None,
          endpoint: str | None = None, error: str | None = None, ms: int | None = None) -> None:
    """Write one file's result. A caption marks its messages dirty for instant search
    and for the passages, so both pick the text up."""
    conn.execute(
        """INSERT INTO media_captions (sha256, state, caption, visible_text, model, endpoint,
                                       prompt_ver, attempts, error, ms, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
           ON CONFLICT (sha256) DO UPDATE SET
             state = excluded.state, caption = excluded.caption,
             visible_text = excluded.visible_text, model = excluded.model,
             endpoint = excluded.endpoint, prompt_ver = excluded.prompt_ver,
             attempts = media_captions.attempts + 1, error = excluded.error,
             ms = excluded.ms, created_at = excluded.created_at""",
        (job.sha256, state, caption, visible_text, model, endpoint, PROMPT_VER, error, ms,
         now_ms()))
    if state == "done":
        mark_dirty(conn, job.messages)


def mark_dirty(conn: sqlite3.Connection, messages: list[tuple[int, int]]) -> None:
    conn.executemany("INSERT OR IGNORE INTO search_dirty (id) VALUES (?)",
                     [(m,) for m, _ in messages])
    conn.executemany("INSERT OR IGNORE INTO passage_dirty (message_id, chat_id) VALUES (?, ?)",
                     messages)


# ---- the model call ------------------------------------------------------------------------

class CaptionError(Exception):
    """This image could not be described: the model refused it, said nothing, or its
    reply was cut off or broken."""


class Waiting(Exception):
    """No model of the Photo descriptions task can answer now; the sentence says why."""


BODY = {"temperature": 0.2, "max_tokens": 500, "response_format": {"type": "json_object"}}


def messages_for(data: bytes, mime: str) -> list[dict[str, Any]]:
    url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    return [{"role": "user", "content": [{"type": "text", "text": prompts.text(__file__, "image.md").rstrip()},
                                         {"type": "image_url", "image_url": {"url": url}}]}]


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def parse_reply(content: str) -> tuple[str, str]:
    """(caption, visible text) from the model's reply: the JSON object in it, or plain
    text as the caption. A JSON object that does not parse (a cut-off reply) raises
    CaptionError."""
    raw = _FENCE.sub("", (content or "").strip())
    if "{" not in raw:
        cap, vis = raw, ""
    else:
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            d = json.loads(m.group(0) if m else raw)
        except ValueError:
            raise CaptionError("The model's reply was incomplete JSON.") from None
        cap = str(d.get("caption") or d.get("description") or "").strip()
        t = d.get("text") or d.get("visible_text") or ""
        vis = "\n".join(map(str, t)) if isinstance(t, list) else str(t)
    vis = vis.strip()
    if vis.lower() in ("none", "n/a", "no text", "\"\"", "''"):
        vis = ""
    return " ".join(cap.split())[:600], vis[:2000]


def run_on(loop: asyncio.AbstractEventLoop | None, coro, timeout: float | None = None):
    """Run `coro` from a worker thread: on the server's loop when it runs (the LLM
    queue lives there), else on a private loop (a script, a test)."""
    if loop is not None and loop.is_running():
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return fut.result(timeout)
        except BaseException:
            fut.cancel()
            raise
    return asyncio.run(coro)


class Engine:
    """Captions through the Photo descriptions task. The worker thread calls
    `caption()`; it runs its coroutine on the server's loop (the LLM queue lives
    there), or on a private loop when there is no server (a script, a test)."""

    def __init__(self, cfg, *, loop: asyncio.AbstractEventLoop | None = None, llm=None):
        self.cfg = cfg
        self.loop = loop
        if llm is None:
            from ..backends.llm import LLM
            llm = LLM(cfg)
        self.llm = llm

    def caption(self, data: bytes, mime: str) -> dict[str, Any]:
        """{caption, visible_text, model, endpoint}. Raises Waiting when no model can
        answer now, CaptionError when this image failed."""
        return run_on(self.loop, self.describe(data, mime))

    async def describe(self, data: bytes, mime: str, *,
                       lane: str = "captions") -> dict[str, Any]:
        """`caption` on the server's loop. Background priority: when no model can
        answer it raises Waiting at once, never waits. A turn that needs a description
        for a model that cannot see images (attachments.py) passes its own lane."""
        from ..backends.base import BackendError, Done, TextDelta
        from ..backends.queue import Priority
        from ..backends.router import NoModel
        from ..settings.thinking import OFF
        qb = self.llm.bind(None, role="vision", priority=Priority.BACKGROUND,
                           lane=lane, body=BODY, thinking=OFF)
        parts: list[str] = []
        done: Done | None = None
        try:
            events = qb.stream(messages_for(data, mime), [])
            try:
                async for ev in events:
                    if isinstance(ev, TextDelta):
                        parts.append(ev.text)
                    elif isinstance(ev, Done):
                        done = ev
                        break
            finally:
                await events.aclose()
        except NoModel as e:
            raise Waiting(e.sentence) from None
        except BackendError as e:
            raise CaptionError(str(e)[:300]) from None
        answered = qb.answered
        model = answered.model
        if done is not None and done.reason == "length":
            raise CaptionError(f"{model} reached its limit of {BODY['max_tokens']} tokens "
                               "before the description was complete.")
        cap, vis = parse_reply("".join(parts))
        if not cap and not vis:
            raise CaptionError(f"{model} gave an empty description")
        return {"caption": cap, "visible_text": vis, "model": model,
                "endpoint": answered.name}


# ---- the worker ---------------------------------------------------------------------------

def run(kb, s: Settings, engine: Engine, ctx: ImportContext, *,
        limit: int | None = None) -> dict[str, Any]:
    """Caption image files, one at a time, until none are left, `limit` are done, or
    the task is cancelled. Ends Unreachable (the job waits for its next run) when no
    model of the task can answer. An image that fails is recorded as failed and the run
    goes on. Resumable: every result is committed as it arrives."""
    from .importers import Unreachable
    d = media_dir(kb)
    res: dict[str, Any] = {"done": 0, "failed": 0, "skipped": 0, "by_model": {},
                           "stopped": "nothing left"}
    tried: set[str] = set()
    queue: list[Job] = []
    started = 0
    gap = 60.0 / s.rate_per_minute if s.rate_per_minute > 0 else 0.0
    last_submit = 0.0
    total_hint = kb.file.read_sync(lambda c: len(pick(c, s, 10**9)))
    if limit:
        total_hint = min(total_hint, limit)
    ctx.progress(0, total_hint, "images")

    def write(job: Job, **kw) -> None:
        kb.file.write_sync(lambda c: store(c, job, **kw))

    def load(job: Job) -> tuple[bytes, str] | None:
        p = d / job.sha256
        if not p.is_file():
            return None
        if p.stat().st_size > s.max_mb * 1024 * 1024:
            write(job, state="skipped", error=f"larger than {s.max_mb:g} MB")
            res["skipped"] += 1
            return None
        data = p.read_bytes()
        mime = sniff(data)
        if mime not in SUPPORTED:
            write(job, state="skipped", error=f"not an image the models read ({mime or 'unknown'})")
            res["skipped"] += 1
            return None
        return data, mime

    while True:
        ctx.check()
        if limit and started >= limit:
            res["stopped"] = "limit reached"
            break
        if not queue:
            queue.extend(kb.file.read_sync(lambda c: pick(c, s, s.batch, tried)))
            if not queue:
                break
        job = queue.pop(0)
        tried.add(job.sha256)
        loaded = load(job)
        if loaded is None:
            continue
        if gap:
            time.sleep(max(0.0, gap - (time.monotonic() - last_submit)))
        last_submit = time.monotonic()
        started += 1
        t0 = time.monotonic()
        try:
            out = engine.caption(*loaded)
        except Waiting as e:
            ctx.progress(res["done"] + res["failed"] + res["skipped"], total_hint, "images")
            raise Unreachable(f"Photo descriptions wait for a model. {e}") from None
        except CaptionError as e:
            write(job, state="failed", error=str(e)[:300])
            res["failed"] += 1
        except Exception as e:                 # noqa: BLE001 - one image never stops the rest
            log.warning("caption failed", exc_info=True,
                        extra={"event": "media.caption_failed", "sha256": job.sha256})
            write(job, state="failed",
                  error=f"Describing this image stopped with an error ({type(e).__name__}).")
            res["failed"] += 1
        else:
            write(job, state="done", caption=out["caption"],
                  visible_text=out["visible_text"], model=out["model"],
                  endpoint=out["endpoint"], ms=int((time.monotonic() - t0) * 1000))
            res["done"] += 1
            res["by_model"][out["model"]] = res["by_model"].get(out["model"], 0) + 1
        ctx.progress(res["done"] + res["failed"] + res["skipped"], total_hint, "images")
    return res


# ---- the app side ------------------------------------------------------------------------

def start(kbs, kb, cfg, *, limit: int | None = None, label: str | None = None,
          loop: asyncio.AbstractEventLoop | None = None, quiet: bool = False):
    """A caption task for one knowledge base (kind "captions"), cancellable like any
    other task. `quiet` (the automatic queue): not listed or announced unless it fails.
    Returns the running one when there is one already."""
    running = kbs.tasks.running(kb.id, "captions")
    if running is not None:
        return running
    s = Settings.parse(cfg.media)
    llm = kbs.llm

    def work(ctx: ImportContext) -> dict[str, Any]:
        return run(kb, s, Engine(cfg, loop=loop, llm=llm), ctx, limit=limit)

    what = f"Describing photos ({limit})" if limit else "Describing photos"
    return kbs.tasks.start(kb.id, "captions", label or what, work, quiet=quiet)


def has_caption_model(cfg) -> bool:
    """Photo descriptions has a model (whether it answers now is the run's business)."""
    st = getattr(cfg, "settings", None)
    return bool(st is not None and st.chain("vision"))


def auto(cfg) -> bool:
    """Describe new chat photos (Settings, General)."""
    st = getattr(cfg, "settings", None)
    return bool(general_mod.value(st.general if st else {}, "image_captions_auto"))


async def run_scheduler(cfg, kbs) -> None:
    """`image_captions_auto` (Settings, General): every `poll_minutes`, a knowledge base with image files
    waiting for a caption gets a caption task, one knowledge base at a time. These
    tasks are quiet: silent unless they fail."""
    loop = asyncio.get_running_loop()
    while True:
        s = Settings.parse(cfg.media)
        try:
            chats = kbs.chats()
            if auto(cfg) and features.on(cfg, "messages") and has_caption_model(cfg) and \
                    not any(kbs.tasks.running(kb.id, "captions") for kb in chats):
                for kb in chats:
                    if await kb.file.read(lambda c: bool(pick(c, s, 1))):
                        start(kbs, kb, cfg, label="Describing photos", loop=loop, quiet=True)
                        break
        except Exception:
            log.exception("caption scheduler check failed")
        await asyncio.sleep(max(60.0, s.poll_minutes * 60))

