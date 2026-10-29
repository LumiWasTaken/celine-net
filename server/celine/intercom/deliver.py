"""The delivery rule. A delivery to a conversation waits until it is
due (no due time, or one that passed). Then:

1. The conversation is idle (no live turn): `deliver_idle` appends every due delivery
   under the active leaf, one message each, and when one of them wakes and the
   conversation may wake (not a stopped mission), queues one turn rooted at the last.
   It runs in the send itself, at every turn end and when a due time passes.
2. A turn is live: nothing outside the runner touches the tree. The runner calls `take`
   before each model request; it appends the due deliveries under the runner's own tail,
   with this turn's id, and returns them for the request.

The delivery row records what happened in the same write that appends the message, so
nothing lands twice and a restart carries on from the table."""
from __future__ import annotations

import json
import sqlite3

from ..conversations import store as conversations, tree
from ..core.ids import now
from ..core.states import LIVE_SQL
from ..core.event_types import INTERCOM_STATE
from .store import notice_user


def author_of(row) -> str:
    """`user` for the user's own message as typed, `intercom` for everything else."""
    return "user" if (row["from_addr"] == "user" and row["kind"] == "message"
                      and not row["due_at"]) else "intercom"


def drop(c: sqlite3.Connection, store, ids: list[str], everyone: bool = False) -> None:
    """What waits to land in the conversations `ids` from anyone but the user (with
    `everyone`, from the user too) is cancelled: a locked conversation takes none of it
    (conversations/store.locked)."""
    marks = ",".join("?" * len(ids))
    for iid, cid in c.execute(
            f"UPDATE intercom_deliveries SET state='cancelled' WHERE state='pending' AND "
            f"conversation_id IN ({marks}) AND intercom_id IN (SELECT id FROM intercom "
            f"WHERE ? OR from_addr <> 'user') RETURNING intercom_id, conversation_id",
            (*ids, int(everyone))).fetchall():
        store.global_event(c, INTERCOM_STATE, {"intercom_id": iid,
                                                 "conversation_id": cid})


def _due(c: sqlite3.Connection, store, cid: str) -> list[sqlite3.Row]:
    if why := conversations.locked(c, cid):
        drop(c, store, [cid], everyone=why == conversations.DELETED)
    return c.execute(
        "SELECT i.* FROM intercom_deliveries d JOIN intercom i ON i.id = d.intercom_id "
        "WHERE d.conversation_id=? AND d.state='pending' AND i.cancelled_at IS NULL "
        "AND (i.due_at IS NULL OR i.due_at <= ?) ORDER BY i.id", (cid, now())).fetchall()


def _live(c: sqlite3.Connection, cid: str) -> bool:
    return c.execute(f"SELECT 1 FROM turns WHERE conversation_id=? AND state IN {LIVE_SQL} "
                     "LIMIT 1", (cid,)).fetchone() is not None


def land(c, store, row, cid: str, parent: str | None, turn_id: str | None) -> dict:
    """One delivery as a message of `cid`. A message to an archived conversation
    brings it back to the list (with Lock archived chats only the user's get there:
    _due)."""
    conversations.set_archived(c, store, cid, False)
    data = json.loads(row["data"] or "{}")
    m = tree.append(c, store, cid, parent, "user", row["text"], author_of(row), row["id"],
                    turn_id, attachments=data.get("attachments") or (),
                    mid=data.get("message_id"))
    c.execute("UPDATE intercom_deliveries SET state='delivered', message_id=?, turn_id=?, "
              "delivered_at=? WHERE intercom_id=? AND conversation_id=? AND state='pending'",
              (m["id"], turn_id, m["created_at"], row["id"], cid))
    # the open pages' lists of scheduled messages
    store.global_event(c, INTERCOM_STATE, {"intercom_id": row["id"],
                                             "conversation_id": cid})
    return m


def deliver_idle_sync(c: sqlite3.Connection, store, cid: str) -> dict | None:
    """Rule 1, inside a write. -> {messages: {intercom id: message id}, turn_id, seq},
    or None when the conversation is busy or nothing is due."""
    if _live(c, cid):
        return None
    rows = _due(c, store, cid)
    if not rows:
        return None
    leaf = c.execute("SELECT active_leaf_id FROM conversations WHERE id=?",
                     (cid,)).fetchone()["active_leaf_id"]
    landed = {}
    for r in rows:
        leaf = land(c, store, r, cid, leaf, None)["id"]
        landed[r["id"]] = leaf
    out = {"messages": landed, "turn_id": None, "seq": None}
    stopped = c.execute("SELECT 1 FROM missions WHERE conversation_id=? AND "
                        "status='stopped'", (cid,)).fetchone()
    if stopped or not any(r["wake"] for r in rows):
        return out
    origin = "user" if any(author_of(r) == "user" for r in rows) else "intercom"
    tid, out["seq"] = tree.queue_turn(c, store, cid, leaf, origin=origin)
    # the turn that answers them: a sub-agent's caller and the answer hook find it here
    c.execute(f"UPDATE intercom_deliveries SET turn_id=? WHERE conversation_id=? AND "
              f"intercom_id IN ({','.join('?' * len(landed))})", (tid, cid, *landed))
    out["turn_id"] = tid
    return out


async def deliver_idle(store, cid: str) -> dict | None:
    return await store.write(lambda c: deliver_idle_sync(c, store, cid))


async def deliver_due(store) -> list[str]:
    """Rule 1 for every conversation with something due, and a scheduled message to the
    user at its time: its notice, once (its delivery's delivered_at marks it). -> the
    turns it queued."""
    def fn(c: sqlite3.Connection) -> list[str]:
        t = now()
        for r in c.execute(
                "UPDATE intercom_deliveries SET delivered_at=? WHERE to_addr='user' AND "
                "conversation_id IS NULL AND state='pending' AND delivered_at IS NULL AND "
                "intercom_id IN (SELECT id FROM intercom WHERE due_at <= ? AND "
                "cancelled_at IS NULL AND from_addr <> 'user' AND kind <> 'job') "
                "RETURNING intercom_id", (t, t)).fetchall():
            notice_user(c, store, c.execute("SELECT * FROM intercom WHERE id=?",
                                            (r[0],)).fetchone())
            store.global_event(c, INTERCOM_STATE, {"intercom_id": r[0],
                                                     "conversation_id": None})
        cids = [r[0] for r in c.execute(
            "SELECT DISTINCT d.conversation_id FROM intercom_deliveries d "
            "JOIN intercom i ON i.id = d.intercom_id WHERE d.state='pending' "
            "AND d.conversation_id IS NOT NULL AND i.cancelled_at IS NULL "
            "AND (i.due_at IS NULL OR i.due_at <= ?)", (now(),))]
        out = []
        for cid in cids:
            r = deliver_idle_sync(c, store, cid)
            if r and r["turn_id"]:
                out.append(r["turn_id"])
        return out

    return await store.write(fn)


async def next_due(store) -> str | None:
    """The earliest due time still waiting, or None."""
    return await store.read(lambda c: c.execute(
        "SELECT min(i.due_at) FROM intercom_deliveries d JOIN intercom i "
        "ON i.id = d.intercom_id WHERE d.state='pending' AND (d.conversation_id IS NOT NULL "
        "OR d.delivered_at IS NULL) AND i.cancelled_at IS NULL AND i.due_at > ?",
        (now(),)).fetchone()[0])


async def take(store, cid: str, turn_id: str, parent: str | None) -> list[dict]:
    """Rule 2: the due deliveries, appended under the turn's tail `parent` with this
    turn's id. -> the messages, oldest first ([] when nothing is due)."""
    def fn(c: sqlite3.Connection) -> list[dict]:
        out, leaf = [], parent
        for r in _due(c, store, cid):
            m = land(c, store, r, cid, leaf, turn_id)
            leaf = m["id"]
            out.append(m)
        return out

    return await store.write(fn)
