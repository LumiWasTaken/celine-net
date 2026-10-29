"""Documents: the AI layer. A short, high-level, non-spoiler description of each file,
written in the background so Celine can judge whether a file matters before reading it.

One task ("describe", cancellable) takes items whose description is
missing or was written for another version of the file, newest first:

- text: up to `describe_part_chars` (8,000) characters go to the model in one call.
  Longer files are map-reduced: the text is cut into parts at segment boundaries, at
  most `describe_max_parts` (12) of them evenly spread over the file are summarised one
  by one (each result is stored in `describe_parts`, so a stopped job resumes mid-file),
  then one call writes the description from those notes.
- images: the media caption engine (kb/media.py, the Photo descriptions task)
  describes the image; the caption becomes the item's text and its AI description.

Every model call waits in the app's LLM queue (backends/queue.py) at background
priority, so a user's turn always goes first. Models: the Titles and descriptions task
in Settings, the first of its models that answers.
When no model is free the task ends ("no model free") and the items stay pending; a
work item (work.py, `documents.describe`) starts the next run once a host of the
models is back, or after work.DEFER_SECONDS, and so does the next upload or rescan.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from typing import Any, Callable

from .. import prompts
from .importers import Cancelled
from .media import run_on

log = logging.getLogger("celine.kb.documents")

PART_CHARS = 8000
MAX_PARTS = 12
MAX_OUT = 1200

# the prompts, beside this module: describe.md (the card), describe_part.md and
# describe_part_late.md (one part of a long file, early and late), describe_check.md
# (the second pass: small models tell the twist even when told not to, but reliably
# delete it when that is the only task)
def _p(name: str) -> str:
    return prompts.text(__file__, f"{name}.md").rstrip()


class Soft(Exception):
    """Nothing wrong with the item: no model free now. The run stops; the item stays."""


class Skip(Exception):
    """The item cannot be described (an image type the models do not read)."""


# ---- the model call ---------------------------------------------------------------------

def default_describer(kb) -> Callable[[str, str], tuple[str, str]]:
    """(system, user) -> (text, model): one request through the LLM queue at
    background priority on the Titles and descriptions models. Soft when none can
    answer now."""
    loop = kb.tasks.loop if kb.tasks is not None else None

    async def one(system: str, user: str) -> tuple[str, str]:
        from ..backends.base import Done, TextDelta
        from ..backends.queue import Priority
        from ..backends.router import NoModel
        from ..settings.thinking import OFF
        llm = kb.llm
        if llm is None:
            raise RuntimeError("describing runs in the app, which holds the LLM queue")
        qb = llm.bind(None, role="small", lane="documents", priority=Priority.BACKGROUND,
                      thinking=OFF)
        parts: list[str] = []
        try:
            events = qb.stream([{"role": "system", "content": system},
                                {"role": "user", "content": user}], [])
            try:
                async for ev in events:
                    if isinstance(ev, TextDelta):
                        parts.append(ev.text)
                        if sum(len(p) for p in parts) > 4 * MAX_OUT:
                            break
                    elif isinstance(ev, Done):
                        break
            finally:
                await events.aclose()
        except NoModel as e:
            raise Soft(e.sentence) from None
        model = getattr(qb.answered, "model", "")
        text = clean("".join(parts))
        if not text:
            raise RuntimeError(f"{model} gave an empty answer")
        return text, model

    def call(system: str, user: str) -> tuple[str, str]:
        return run_on(loop, one(system, user), 900)
    return call


def default_captioner(kb) -> Callable[[bytes, str], dict[str, Any]]:
    """An image file's caption through the Photo descriptions task (kb/media.py)."""
    from . import media
    s = media.Settings.parse(kb.app_cfg.media)
    engine = media.Engine(kb.app_cfg, loop=kb.tasks.loop if kb.tasks is not None else None,
                          llm=kb.llm)

    def call(data: bytes, mime: str) -> dict[str, Any]:
        if len(data) > s.max_mb * 1024 * 1024:
            raise Skip(f"larger than {s.max_mb:g} MB")
        try:
            return engine.caption(data, mime)
        except media.Waiting as e:
            raise Soft(str(e)) from None
    return call


_THINK = re.compile(r"<think>.*?</think>", re.S)


def clean(text: str) -> str:
    t = _THINK.sub("", text or "").strip()
    t = re.sub(r"^(?:\*\*)?(?:description|catalogue card|card|notes)(?:\*\*)?\s*:\s*", "", t,
               flags=re.I)
    t = t.strip().strip('"“”').strip()
    t = " ".join(t.split())
    if len(t) > MAX_OUT:
        cut = t.rfind(". ", 0, MAX_OUT)
        t = t[:cut + 1] if cut > MAX_OUT // 2 else t[:MAX_OUT].rstrip() + "…"
    return t


# ---- what goes to the model -------------------------------------------------------------------

def _about(it: dict[str, Any]) -> str:
    from .documents import size_line
    lines = [f"File: {it['path']}", f"Kind: {size_line(it)}"]
    if it.get("user_title") or it.get("file_title"):
        lines.append(f"Title: {it.get('user_title') or it.get('file_title')}")
    if it.get("user_description"):
        lines.append(f"The user's own description: {it['user_description']}")
    return "\n".join(lines)


def _seg_text(r: sqlite3.Row) -> str:
    return f"{r['speaker']}: {r['text']}" if r["speaker"] else r["text"]


def parts_of(rows: list[sqlite3.Row], part_chars: int, max_parts: int) -> tuple[list[str], int]:
    """(chosen parts, total parts). Parts end at segment boundaries; when there are more
    than `max_parts`, an even spread over the file is taken (first part always)."""
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for r in rows:
        t = _seg_text(r)
        if buf and size + len(t) > part_chars:
            parts.append("\n".join(buf))
            buf, size = [], 0
        buf.append(t[:part_chars])
        size += len(t) + 1
    if buf:
        parts.append("\n".join(buf))
    n = len(parts)
    if n <= max_parts:
        return parts, n
    step = n / max_parts
    picked = sorted({int(i * step) for i in range(max_parts)})
    return [parts[i] for i in picked], n


def describe_text(kb, it: dict[str, Any], call, ctx) -> tuple[str, str]:
    """The description of a text item: written, then checked for spoilers."""
    text, model = _draft(kb, it, call, ctx)
    if not text or not kb.opt("describe_spoiler_check", True):
        return text, model
    ctx.check()
    checked, _ = call(_p("describe_check"), text)
    # a check that lost most of the card (or answered something else) is not taken
    return (checked if checked and len(checked) >= 0.3 * len(text) else text), model


def _draft(kb, it: dict[str, Any], call, ctx) -> tuple[str, str]:
    part_chars = int(kb.opt("describe_part_chars", PART_CHARS))
    max_parts = int(kb.opt("describe_max_parts", MAX_PARTS))
    rows = kb.file.read_sync(lambda c: c.execute(
        "SELECT kind, speaker, text FROM segments WHERE item_id = ? ORDER BY seq",
        (it["id"],)).fetchall())
    about = _about(it)
    chosen, total = parts_of(rows, part_chars, max_parts)
    if total <= 1:
        # the model is shown the beginning only: a twist it never read cannot leak.
        # A file of one or two passages (a recipe, a note) is shown whole.
        texts = [_seg_text(r) for r in rows]
        full = sum(len(t) for t in texts)
        shown, used = [], 0
        for t in texts:
            if shown and len(texts) > 2 and used + len(t) > 0.6 * full:
                break
            shown.append(t)
            used += len(t)
        rest = "" if len(shown) == len(texts) else \
            (f"\n[… the file goes on for about "
             f"{sum(len(t.split()) for t in texts[len(shown):]):,} more words, not shown]")
        return call(_p("describe"), f"{about}\n\n--- content ---\n" + "\n".join(shown) + rest)
    sha = it["sha256"]
    have = kb.file.read_sync(lambda c: {r[0]: r[1] for r in c.execute(
        "SELECT part, text FROM describe_parts WHERE item_id = ? AND source = ?",
        (it["id"], sha))})
    notes: list[str] = []
    model = ""
    for i, part in enumerate(chosen):
        ctx.check()
        if i in have:
            notes.append(have[i])
            continue
        system = _p("describe_part" if i < max(1, len(chosen) // 2) else "describe_part_late")
        text, model = call(system, f"{about}\nPart {i + 1} of the {len(chosen)} parts "
                                       f"read (the file has {total}).\n\n--- part ---\n{part}")
        notes.append(text)
        kb.file.write_sync(lambda c, i=i, text=text: c.execute(
            "INSERT OR REPLACE INTO describe_parts (item_id, source, part, text) "
            "VALUES (?, ?, ?, ?)", (it["id"], sha, i, text)))
    joined = "\n".join(f"{i + 1}. {n}" for i, n in enumerate(notes))
    note = "" if len(chosen) == total else \
        f" ({len(chosen)} parts spread over the whole file were read)"
    return call(_p("describe"), f"{about}\n\nThe file is long; here are notes on its parts, in "
                        f"order{note}:\n{joined}")


# ---- the task ---------------------------------------------------------------------------------

def _next(kb, skip: set[int]) -> dict[str, Any] | None:
    iid = kb.file.read_sync(lambda c: next((r[0] for r in c.execute(
        "SELECT id FROM items WHERE state = 'ready' AND ai_state = 'pending' "
        "ORDER BY MAX(updated_at, mtime) DESC") if r[0] not in skip), None))
    return kb.item(iid) if iid is not None else None


def _store(kb, it: dict[str, Any], **cols: Any) -> None:
    sets = ", ".join(f"{k} = ?" for k in cols)

    def run(conn: sqlite3.Connection) -> None:
        # only when the file is still the one that was described
        n = conn.execute(f"UPDATE items SET {sets} WHERE id = ? AND sha256 = ?",
                         [*cols.values(), it["id"], it["sha256"]]).rowcount
        if n:
            kb.index_meta(conn, it["id"])
    kb.file.write_sync(run)


def _again(kb) -> None:
    """No model answered: the work queue starts the next run (one waiting item)."""
    loop = kb.tasks.loop if kb.tasks is not None else None
    if kb.work is None or loop is None:
        return
    from .. import work
    run_on(loop, kb.work.put("documents.describe", {"kb": kb.id}, unique=True,
                             after=work.DEFER_SECONDS), 30)


def run(kb, ctx) -> dict[str, Any]:
    describer = kb.describer
    captioner = kb.captioner
    total = kb.file.read_sync(lambda c: c.execute(
        "SELECT COUNT(*) FROM items WHERE state = 'ready' AND ai_state = 'pending'"
    ).fetchone()[0])
    res: dict[str, Any] = {"described": 0, "failed": 0, "skipped": 0}
    ctx.progress(0, total, "files")
    tried: set[int] = set()
    while True:
        ctx.check()
        it = _next(kb, tried)
        if it is None:
            break
        tried.add(it["id"])
        t0 = time.monotonic()
        try:
            if it["kind"] == "image":
                if captioner is None:
                    captioner = default_captioner(kb)
                from .media import SUPPORTED, sniff
                data = (kb.root / it["path"]).read_bytes()
                mime = sniff(data)
                if mime not in SUPPORTED:
                    raise Skip(f"not an image the models read ({mime or it['mime'] or 'unknown'})")
                out = captioner(data, mime)
                kb.add_caption(it["id"], out["caption"], out.get("visible_text") or "",
                               out.get("model") or "")
                text, model = out["caption"], out.get("model") or ""
            else:
                if describer is None:
                    describer = default_describer(kb)
                text, model = describe_text(kb, it, describer, ctx)
            if not text:
                raise RuntimeError("the model gave an empty description")
            _store(kb, it, ai_description=text, ai_model=model, ai_generated_at=int(
                time.time() * 1000), ai_source_hash=it["sha256"], ai_state="done",
                ai_error=None)
            res["described"] += 1
            log.info("document described", extra={
                "event": "documents.described", "kb": kb.id, "item": it["id"], "model": model,
                "ms": int((time.monotonic() - t0) * 1000)})
        except Cancelled:
            raise
        except Soft as e:
            res["stopped"] = str(e)[:300]
            _again(kb)
            break
        except Skip as e:
            _store(kb, it, ai_state="skipped", ai_error=str(e)[:300])
            res["skipped"] += 1
        except Exception as e:                 # noqa: BLE001 - one file never stops the rest
            _store(kb, it, ai_error=f"{type(e).__name__}: {e}", ai_state="failed")
            res["failed"] += 1
            log.warning("document description failed", extra={
                "event": "documents.describe_failed", "kb": kb.id, "item": it["id"],
                "error": f"{type(e).__name__}: {e}"[:300]})
        kb.item_event(it["id"])
        ctx.progress(res["described"] + res["failed"] + res["skipped"], total, "files")
    return res
