"""History and Ask Celine for every long text (web/src/lib/texts.ts, server/celine/texts.py).

Ask Celine: the user writes a draft for one text, on the chat role, as one request with
thinking off. The draft streams back on the request that asked, to that editor alone, as
lines of JSON: `{text}` what it added, then `{done, text, error}` with the whole draft.
It reads the text as it stands and what it belongs to (`about`), read only. Nothing is
stored, nothing goes on the feed: the editor puts the draft into its text with Copy,
Insert or Replace. A failure is the last line's `error`; nothing falls back. The editor
going away ends the request, and the model's work with it."""
from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import texts
from ..backends.base import BackendError, Done, TextDelta
from ..errors import CelineError, NotFound
from .deps import services_of

router = APIRouter(prefix="/api")
log = logging.getLogger("celine.texts")
EVERY = 0.1          # seconds between two lines while the draft streams


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


def _line(**data) -> str:
    return json.dumps(data, ensure_ascii=False) + "\n"


async def _draft(backend, messages: list[dict]):
    """The response's lines: what the draft added, at most every EVERY seconds, then the
    whole draft with done (and the error, when it failed)."""
    parts: list[str] = []
    sent, last = 0, 0.0
    events = backend.stream(messages, [])
    try:
        try:
            async for ev in events:
                if isinstance(ev, TextDelta):
                    parts.append(ev.text)
                    if time.monotonic() - last >= EVERY:
                        last = time.monotonic()
                        added, sent = "".join(parts[sent:]), len(parts)
                        yield _line(text=added)
                elif isinstance(ev, Done):
                    break
            end = _line(done=True, text="".join(parts).strip(), error=None)
        except BackendError as e:
            end = _line(done=True, text="".join(parts), error=str(e))
        except Exception as e:      # the editor waits for the last line: it must hear the end
            log.exception("texts.ask failed")
            end = _line(done=True, text="".join(parts), error=str(e) or type(e).__name__)
        yield end
    finally:                        # frees a queued backend's model slot right away
        if (aclose := getattr(events, "aclose", None)) is not None:
            await aclose()


@router.post("/text-drafts")
async def ask(request: Request, body: AskBody) -> StreamingResponse:
    """{of, prompt, text} -> its draft, streamed as lines of JSON (_draft)."""
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
    return StreamingResponse(_draft(backend, _messages(cfg, body, lines)),
                             media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
