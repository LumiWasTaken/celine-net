"""A title for a conversation from its first exchange."""
from __future__ import annotations

import logging
import re

from .. import prompts, work
from ..conversations import store as conversations
from .compact import complete

log = logging.getLogger("celine.title")

SECONDS = 30        # a title that takes longer is not waited for
WORDS = 4           # a title is 2 to 4 words; a longer line is cut to four


async def title_for(backend, user: str, reply: str, errors: bool = False) -> str:
    """`errors`: a backend error is raised (compact.complete), else it is no title."""
    body = f"First message:\n{user[:1500]}\n\nReply:\n{reply[:1500]}"
    text = (await complete(backend, [{"role": "system", "content": prompts.text(__file__, "title.md")},
                                     {"role": "user", "content": body}], SECONDS,
                           errors=errors)
            or "")[:200].strip()
    line = text.splitlines()[0] if text else ""
    # a line ending in a colon is a label echoed from the prompt ("First message:"), not
    # a title: the caller falls back to the user's own words
    return "" if line.endswith(":") else line


def plain(title: str) -> str:
    """A title as plain words on one line, at most WORDS of them (the model's, and the
    user's first line when no model answered): markdown's emphasis and code marks and a
    heading's # go, and the quotes and punctuation around it."""
    return " ".join(re.sub(r"[*`]+|^#+", "", title).split()[:WORDS]).strip(' "\'.,:;?!')


async def make(store, cid: str, backend_for) -> None:
    """A short title for `cid` from its first exchange, once: from the small model,
    else from the chat model (`backend_for(role)`, None without one), else the first
    line of the first message. When no model answered at all and one could not (no
    host up, work.deferrable), it raises that error instead: the work item is deferred
    until a host is back. A title the conversation has stays."""
    if await conversations.title(store, cid):
        return
    user, reply = await conversations.first_exchange(store, cid)
    if not user:
        return
    title, soft, answered = "", None, False
    for role in ("small", "chat"):
        try:
            backend = backend_for(role)
            if backend is not None:
                title = plain(await title_for(backend, user, reply, errors=True))
                answered = True
        except Exception as e:
            if work.deferrable(e):
                soft = e
            log.info("title call failed", extra={"event": "title.call_failed",
                                                 "role": role, "error": type(e).__name__})
        if title:
            break
    if not title and not answered and soft is not None:
        raise soft
    title = title or plain(user.strip().splitlines()[0][:60])
    await conversations.update(store, cid, title=title[:80], auto=True)
