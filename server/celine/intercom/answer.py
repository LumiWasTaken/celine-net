"""The agent's answer to the user's reply in a thread. When the user answers a message in the
Intercom (or quote-replies in a bridged chat), the turn that takes it in answers in its
own conversation. When that turn is done and sent nothing into the thread after the
reply, its final answer goes into the thread as its message to the user, so the thread
reads like a chat. A sub-agent's answer is its report (sub_agents/report.py)."""
from __future__ import annotations

import sqlite3

from ..core.ids import now
from ..conversations.tree import final_words
from . import store as rows


def _asked(c: sqlite3.Connection, cid: str, turn_id: str) -> sqlite3.Row | None:
    """The user's newest reply in a thread that turn `turn_id` of `cid` took in, when
    the turn is done and nothing from `cid` came into that thread after it."""
    if not c.execute("SELECT 1 FROM turns t JOIN conversations k ON k.id = t.conversation_id "
                     "WHERE t.id = ? AND t.state = 'done' AND k.agent_of IS NULL",
                     (turn_id,)).fetchone():
        return None
    return c.execute(
        "SELECT i.id, json_extract(i.data, '$.via') AS via FROM intercom i "
        "JOIN intercom_deliveries d ON d.intercom_id = i.id WHERE d.turn_id = ? AND "
        "d.conversation_id = ? AND i.from_addr = 'user' AND i.reply_to IS NOT NULL AND "
        "NOT EXISTS (SELECT 1 FROM intercom a WHERE a.thread = i.thread AND "
        "a.from_addr = ? AND a.id > i.id AND a.cancelled_at IS NULL AND "
        "(a.due_at IS NULL OR a.due_at <= ?)) ORDER BY i.id DESC LIMIT 1",
        (turn_id, cid, f"agent:{cid}", now())).fetchone()


async def on_end(services, cid: str, turn_id: str | None) -> None:
    """The dispatcher's turn-end hook (module docstring). An answer to a bridged chat's
    reply is read there already: it lands read."""
    if turn_id is None:
        return
    store = services.store
    asked, text = await store.read(lambda c: (
        (r := _asked(c, cid, turn_id)), final_words(c, turn_id) if r is not None else ""))
    if asked is None or not text:
        return
    out = await services.intercom.send(f"agent:{cid}", "", text, reply_to=asked["id"])
    if out and asked["via"]:
        await store.write(lambda c: rows.read(c, [out["intercom_id"]]))
