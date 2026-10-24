"""The small role as Bastet's judges and reader use it: one user message in, what the
model wrote out (thinking off, exactly the role's first model, never a fallback), and
the first JSON object in it. Every guard module that asks the small role asks here.

On (`background` False) the request waits in the LLM queue at the priority of its turn,
its queue wait on its clock like its own model waits (the vision.py pattern). Watch and
the patrol (`background` True) run behind every turn, on nobody's clock, and leave its
turn's priority as it is. Unavailable: no model of the role can answer now.
"""
from __future__ import annotations

import json
import re

from ..backends.base import BackendError, Done, TextDelta
from ..backends.queue import Priority
from ..settings.thinking import OFF

ROLE = "small"


class Unavailable(Exception):
    """No model can answer now; the call waits and asks again (On), or its row says
    unreachable (Watch)."""


async def complete(llm, text: str, body: dict, *, turn: dict | None = None,
                   conversation_id: str | None = None,
                   background: bool = False) -> tuple[str, dict | None, str]:
    """(what the model wrote, its raw reply, the model) for one user message. `turn`
    (id, conversation_id): the turn it is asked in, None for the owner's own answer
    (the banner reader)."""
    if llm is None:
        raise Unavailable("the app's model layer is not running.")
    tid = turn["id"] if turn and not background else None
    qb = llm.bind(None, role=ROLE,
                  priority=Priority.BACKGROUND if background else
                  llm.turn_priority.get(tid or "", Priority.AGENT),
                  conversation_id=turn["conversation_id"] if turn else conversation_id,
                  turn_id=tid, body=body,
                  thinking=OFF, voice=False, fallback=False, label="Guard")
    parts: list[str] = []
    raw = None
    try:
        events = qb.stream([{"role": "user", "content": text}], [])
        try:
            async for ev in events:
                if isinstance(ev, TextDelta):
                    parts.append(ev.text)
                elif isinstance(ev, Done):
                    raw = ev.raw
                    break
        finally:
            await events.aclose()
    except BackendError as e:
        raise Unavailable(str(e)[:300]) from None
    return "".join(parts), raw, getattr(qb.answered, "model", "") or ""


def parse_json(said: str) -> dict:
    """The first JSON object in what a model wrote; {} when there is none."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", said or ""):
        try:
            v, _ = dec.raw_decode(said, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(v, dict):
            return v
    return {}
