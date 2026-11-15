"""Who may see a conversation: its class (`conversations.privacy`, a notebook class),
the strictest of its exchanges'. After each turn a work item (`privacy`) asks the First
look two yes/no questions (questions.toml [privacy]), or the guard role for one word
(privacy.md) without one: intimate is nsfw, private sensitive, else informal. A class
only rises; one the owner set (`privacy_owner`) is never classed over. Until classed, or
with no model to ask, a conversation is sensitive: a missing answer never shares it.
past_conversations' results carry it (facts.result_class), so the leak rule and its
gates take it from there.
"""
from __future__ import annotations

import logging
import sqlite3

from . import prompts
from .facts import strictest
from .questions import FLAG
from .small_role import (Unanswerable, Unavailable, Unreadable, complete, decide, decider,
                         p_yes)

log = logging.getLogger(__name__)

UNCLASSED = "sensitive"          # a conversation not classed yet
WORDS = {"shareable": "informal", "private": "sensitive", "intimate": "nsfw"}
EXCHANGE = 6000                  # characters of the exchange it reads, the end kept


def of(c: sqlite3.Connection, cids) -> str:
    """The strictest class of conversations `cids`."""
    cids = list(dict.fromkeys(cids))
    if not cids:
        return UNCLASSED
    rows = c.execute(f"SELECT privacy FROM conversations WHERE id IN "
                     f"({','.join('?' * len(cids))})", cids).fetchall()
    return strictest([r[0] or UNCLASSED for r in rows]) or UNCLASSED


def _exchange(c: sqlite3.Connection, cid: str, tid: str) -> str | None:
    """What was said in turn `tid`, None for one of a conversation the owner classed."""
    if c.execute("SELECT 1 FROM conversations WHERE id=? AND privacy_owner=0",
                 (cid,)).fetchone() is None:
        return None
    rows = c.execute("SELECT role, content FROM messages WHERE turn_id=? AND role IN "
                     "('user', 'assistant') AND coalesce(content, '') <> '' "
                     "ORDER BY created_at, rowid", (tid,)).fetchall()
    root = c.execute("SELECT m.content FROM turns t JOIN messages m ON m.id = "
                     "t.root_message_id WHERE t.id=?", (tid,)).fetchone()
    said = ([f"user: {root[0]}"] if root and root[0] else []) + [
        f"{r[0]}: {r[1]}" for r in rows if r[0] == "assistant"]
    return "\n\n".join(said)[-EXCHANGE:] or None


async def classify(app, cid: str, tid: str) -> str | None:
    """The class of turn `tid`'s exchange, written to its conversation (raised, never
    lowered); None when there is nothing to class or no model answered."""
    store, cfg, llm = app.store, app.cfg, app.llm
    text = await store.read(lambda c: _exchange(c, cid, tid))
    if text is None:
        return None
    turn = {"id": tid, "conversation_id": cid}
    try:
        if decider(llm):
            qs = prompts.questions()["privacy"]
            got, _ = await decide(llm, text, {n: {"type": "noul",
                                                  "instructions": cfg.substitute(q)}
                                              for n, q in qs.items()},
                                  turn=turn, background=True)
            cls = ("nsfw" if p_yes(got["intimate"]) >= FLAG else
                   "sensitive" if p_yes(got["private"]) >= FLAG else "informal")
        else:
            said, _, model = await complete(
                llm, cfg.substitute(prompts.text("privacy"), braces=True).format(
                    exchange=text), {}, turn=turn, background=True)
            word = next((w for w in said.lower().split() if w.strip(".") in WORDS), "")
            if not word:
                raise Unreadable(said[:300], model)
            cls = WORDS[word.strip(".")]
    except (Unavailable, Unanswerable, Unreadable) as e:
        log.info("privacy not classed", extra={"event": "guard.privacy_skipped",
                                               "conversation": cid, "why": str(e)})
        return None

    def write(c: sqlite3.Connection) -> str:
        row = c.execute("SELECT privacy FROM conversations WHERE id=? AND "
                        "privacy_owner=0", (cid,)).fetchone()
        if row is None:
            return cls
        new = strictest([cls] + ([row[0]] if row[0] else []))
        c.execute("UPDATE conversations SET privacy=? WHERE id=?", (new, cid))
        return new
    return await store.write(write)


def start(app) -> None:
    """At startup: a `privacy` work item after every turn that ended."""
    pool = app.dispatcher.work

    async def ended(cid: str, tid: str | None) -> None:
        if tid is not None:
            await pool.put("privacy", {"conversation_id": cid, "turn_id": tid},
                           conversation_id=cid)
    app.dispatcher.on_end(ended)
    pool.handle("privacy", (), lambda p: classify(app, p["conversation_id"], p["turn_id"]))
