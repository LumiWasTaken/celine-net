"""The guard role as Bastet's judges and reader use it: one user message in, what the
model wrote out (thinking off, exactly the role's first model, never a fallback), and
the first JSON object in it. Every guard module that asks the guard role asks here.

On (`background` False) the request waits in the LLM queue at the priority of its turn,
its queue wait on its clock like its own model waits (the vision.py pattern). Watch and
the patrol (`background` True) run behind every turn, on nobody's clock, and leave its
turn's priority as it is. Unavailable: no model of the role can answer now; Unanswerable:
the model answered with an error a retry does not change.
"""
from __future__ import annotations

import json
import re

from ..backends.base import BackendError, text_of
from ..backends.queue import Priority
from ..settings.thinking import OFF

ROLE = "guard"


class Unavailable(Exception):
    """No model can answer now; the call waits and asks again (On), or its row says
    unreachable (Watch)."""


class Unanswerable(Exception):
    """The model was asked and cannot answer this at all (the request does not fit, it
    takes no such input, no credits, or another error a retry does not fix): the call
    fails with its reason instead of waiting."""


HARD = ("context_overflow", "unsupported_input", "out_of_credits")


class Unreadable(Exception):
    """The model answered outside the words it was asked for: nothing passes on it. The
    exception's text is what the model wrote; the row keeps it as its why, and `model`
    as the model that wrote it."""

    def __init__(self, said: str, model: str = ""):
        super().__init__(said)
        self.model = model


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
    try:
        said, done = await text_of(qb.stream([{"role": "user", "content": text}], []))
    except BackendError as e:
        raise _why(e) from None
    return said, done.raw if done else None, getattr(qb.answered, "model", "") or ""


def _why(e: BackendError) -> Exception:
    """A model error as Bastet waits on it: Unanswerable for an answer a retry cannot
    change, else Unavailable (no model now, a timeout, a busy server, a Stop)."""
    from ..backends.queue import QueueCancelled
    from ..backends.router import NoModel
    said = str(e)[:300]
    if e.code in HARD or not (e.retryable or isinstance(e, (NoModel, QueueCancelled))):
        return Unanswerable(said)
    return Unavailable(said)


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
