"""A title for a conversation from its first exchange: the first line the small model
wrote, as it wrote it."""
from __future__ import annotations

import logging

from .. import prompts, work
from ..conversations import store as conversations
from .compact import complete

log = logging.getLogger("celine.title")

SECONDS = 30        # a title that takes longer is not waited for


async def title_for(backend, user: str, reply: str, errors: bool = False) -> str:
    """The first line of what the model wrote, as it came; empty when it wrote nothing.
    `errors`: a backend error is raised (compact.complete), else it is no title."""
    body = f"First message:\n{user[:1500]}\n\nReply:\n{reply[:1500]}"
    text = await complete(backend, [{"role": "system",
                                     "content": prompts.text(__file__, "title.md")},
                                    {"role": "user", "content": body}], SECONDS,
                          errors=errors) or ""
    return text.split("\n", 1)[0]


async def make(store, cid: str, backend_for) -> None:
    """A title for `cid` from its first exchange, once, from the small model
    (`backend_for("small")`). A failure leaves the conversation without a title and logs
    why; when the model could not answer because no host is up (work.deferrable), it
    raises that error instead: the work item is deferred until a host is back. A title
    the conversation has stays."""
    if await conversations.title(store, cid):
        return
    user, reply = await conversations.first_exchange(store, cid)
    if not user:
        return
    backend = backend_for("small")
    if backend is None:
        log.warning("no title: no model", extra={"event": "title.failed", "conversation": cid,
                                                 "error": "no small model"})
        return
    try:
        title = await title_for(backend, user, reply, errors=True)
    except Exception as e:
        if work.deferrable(e):
            raise
        log.warning("no title: the model failed",
                    extra={"event": "title.failed", "conversation": cid,
                           "error": f"{type(e).__name__}: {e}"[:300]})
        return
    if not title.strip():
        log.warning("no title: the model wrote none",
                    extra={"event": "title.failed", "conversation": cid, "error": "empty"})
        return
    await conversations.update(store, cid, title=title, auto=True)
