"""History and Ask Celine for every long text (web/src/lib/texts.ts, server/celine/texts.py).

Ask Celine: the user writes a draft for one text, on the chat role, as one request with
thinking off. The draft streams as `texts.draft {id, text, done, error}` on the global
feed, the whole draft so far each time. It reads the text as it stands and what it
belongs to (`about`), read only. Nothing is stored: the editor puts the draft into its
text with Copy, Insert or Replace. A failure is the event's `error`; nothing falls back."""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from .. import texts
from ..backends.base import BackendError, Done, TextDelta
from ..core.ids import new_id
from ..errors import CelineError, NotFound
from ..core.event_types import TEXTS_DRAFT
from .deps import services_of

router = APIRouter(prefix="/api")
log = logging.getLogger("celine.texts")
EVERY = 0.1          # seconds between two draft events while it streams
_running: set[asyncio.Task] = set()


@router.get("/text-versions")
async def history(request: Request, of: str, q: str = "", before: int | None = None) -> dict:
    s = services_of(request)
    return await s.store.read(lambda c: texts.history(c, s.cfg, of, q, before))


@router.get("/text-versions/{vid}")
async def version(request: Request, vid: int) -> dict:
    s = services_of(request)
    v = await s.store.read(lambda c: texts.version(c, s.cfg, vid))
    if v is None:
        raise NotFound("version")
    return v


@router.post("/text-versions/{vid}/revert")
async def revert(request: Request, vid: int) -> dict:
    """Version `vid` becomes the text again (her.revert). -> {of, text}."""
    from .. import her
    return await her.revert(services_of(request), vid)


# ---- Ask Celine -------------------------------------------------------------------------

class AskBody(BaseModel):
    of: str = Field(max_length=300)
    prompt: str = Field(min_length=1, max_length=8000)
    text: str = Field(default="", max_length=400_000)


def _messages(cfg, body: AskBody, lines: list[str]) -> list[dict]:
    from ..skills import guide
    system = "\n\n".join(p for p in (
        cfg.substitute(cfg.her.persona).strip(),
        cfg.substitute(guide("texts").body),
        "\n".join(lines),
        f"The text as it stands:\n\n{body.text}" if body.text.strip()
        else "The text is empty.") if p)
    return [{"role": "system", "content": system},
            {"role": "user", "content": body.prompt}]


async def _draft(store, backend, aid: str, messages: list[dict]) -> None:
    def emit(text: str, done: bool = False, error: str | None = None) -> None:
        store.emit_global({"type": TEXTS_DRAFT, "id": aid, "text": text,
                           "done": done, "error": error})
    parts: list[str] = []
    last = 0.0
    events = backend.stream(messages, [])
    try:
        async for ev in events:
            if isinstance(ev, TextDelta):
                parts.append(ev.text)
                if time.monotonic() - last >= EVERY:
                    last = time.monotonic()
                    emit("".join(parts))
            elif isinstance(ev, Done):
                break
        emit("".join(parts).strip(), done=True)
    except BackendError as e:
        emit("".join(parts), done=True, error=str(e))
    except Exception as e:          # the editor waits on this id: it must hear the end
        log.exception("texts.ask failed")
        emit("".join(parts), done=True, error=str(e) or type(e).__name__)
    finally:                        # frees a queued backend's model slot right away
        if (aclose := getattr(events, "aclose", None)) is not None:
            await aclose()


@router.post("/text-drafts")
async def ask(request: Request, body: AskBody) -> dict:
    """{of, prompt, text} -> {id}: its draft comes as `texts.draft` events with this id."""
    from ..backends.queue import Priority
    s = services_of(request)
    cfg = s.cfg
    factory = getattr(s.dispatcher, "backend_factory", None)
    fixed = factory() if factory is not None else None
    backend = s.llm.bind(fixed, role="chat", priority=Priority.INTERACTIVE, thinking="off",
                         voice=False, fallback=False,
                         label=f"Ask {cfg.name}") if s.llm is not None else fixed
    if backend is None:
        raise CelineError("unavailable", "No chat model is set up.", 503)
    lines = await s.store.read(lambda c: texts.about(c, cfg, body.of))
    aid = new_id()
    task = asyncio.create_task(_draft(s.store, backend, aid, _messages(cfg, body, lines)))
    _running.add(task)
    task.add_done_callback(_running.discard)
    return {"id": aid}
