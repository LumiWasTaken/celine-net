"""Compaction rows and the aging mark: plain functions over the store, and the one
read the context builder (turns/context.py) starts from."""
from __future__ import annotations

import json
import sqlite3

from .. import attachments as files
from ..conversations import tree
from ..core.ids import new_id, now
from ..core.event_types import CONTEXT_AGED, CONTEXT_COMPACTED


def rows(c: sqlite3.Connection, cid: str) -> list[dict]:
    return [dict(r) for r in c.execute(
        "SELECT * FROM compactions WHERE conversation_id=? ORDER BY created_at, rowid",
        (cid,))]


def as_view(r: dict) -> dict:
    """What the UI gets for a divider (snapshot `compactions`, `context.compacted`)."""
    return {"id": r["id"], "trigger": r["trigger"],
            "through_message_id": r["through_message_id"],
            "covers_messages": r["covers_messages"], "text": r["text"],
            "tokens_before": r["tokens_before"], "tokens_after": r["tokens_after"],
            "focus": r["focus"], "created_at": r["created_at"]}


async def context_view(store, cid: str, path: list[dict] | None = None) -> dict:
    """What the context builder needs, in one read: the active path (root first; `path`
    when the turn read it already) with every reply's calls and their stored results,
    the aging mark (`aged`, a message id) and the compactions that are valid on this
    path (oldest first)."""
    def fn(c: sqlite3.Connection) -> dict:
        chain = path if path is not None else tree.path(c, cid)
        ids = [m["id"] for m in chain]
        calls: dict[str, list[dict]] = {}
        if ids:
            marks = ",".join("?" * len(ids))
            for r in c.execute(
                    f"SELECT tc.id, tc.message_id, tc.name, tc.arguments, tc.state, "
                    f"tc.display, tc.result_message_id, r.content AS result "
                    f"FROM tool_calls tc LEFT JOIN messages r "
                    f"ON r.id = tc.result_message_id "
                    f"WHERE tc.message_id IN ({marks}) ORDER BY tc.rowid", ids):
                call = dict(r)
                call["display"] = json.loads(r["display"]) if r["display"] else None
                # images the result holds (skills ToolResult.images)
                call["attachments"] = files.of_message(c, r["result_message_id"]) \
                    if r["result_message_id"] else []
                calls.setdefault(r["message_id"], []).append(call)
        st = c.execute("SELECT aged_through FROM context_state WHERE conversation_id=?",
                       (cid,)).fetchone()
        aged = st[0] if st else None
        # each user message's block (turns/message_context.py) and each reply's signed
        # thinking (meta `thinking`), in one query each
        marks = ",".join("?" * len(ids))
        context = dict(c.execute(f"SELECT message_id, text FROM message_context WHERE "
                                 f"message_id IN ({marks})", ids).fetchall()) if ids else {}
        signed = dict(c.execute(f"SELECT id, json_extract(meta, '$.thinking') FROM "
                                f"messages WHERE id IN ({marks}) AND role='assistant'",
                                ids).fetchall()) if ids else {}
        for m in chain:
            m["context"] = context.get(m["id"])
            if m["role"] == "assistant":
                m["thinking"] = json.loads(signed[m["id"]]) if signed.get(m["id"]) else None
        on_path = set(ids)
        return {"chain": chain, "calls": calls, "aged": aged,
                "compactions": [r for r in rows(c, cid)
                                if r["through_message_id"] in on_path]}

    return await store.read(fn)


async def set_aged_through(store, cid: str, message_id: str, event: dict) -> int:
    """Move the aging mark and log `context.aged` in the same write."""
    def fn(c: sqlite3.Connection) -> int:
        c.execute("INSERT INTO context_state(conversation_id, aged_through, updated_at) "
                  "VALUES(?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET "
                  "aged_through=excluded.aged_through, updated_at=excluded.updated_at",
                  (cid, message_id, now()))
        return store.event(c, cid, CONTEXT_AGED,
                           {"through_message_id": message_id, **event})

    return await store.write(fn)


async def save(store, cid: str, row: dict) -> dict:
    """Store a compaction and append `context.compacted` (the divider) in one
    write. -> the compaction as the UI sees it, plus `seq`."""
    comp_id = new_id()

    def fn(c: sqlite3.Connection) -> dict:
        if c.execute("SELECT 1 FROM messages WHERE id=? AND conversation_id=?",
                     (row["through_message_id"], cid)).fetchone() is None:
            raise KeyError(row["through_message_id"])
        full = {"id": comp_id, "conversation_id": cid, "text": "",
                "tokens": 0, "tokens_before": None, "tokens_after": None,
                "focus": None, "model": None, "created_at": now(), **row}
        cols = ["id", "conversation_id", "through_message_id", "trigger",
                "covers_messages", "text", "tokens", "tokens_before", "tokens_after",
                "focus", "model", "created_at"]
        c.execute(f"INSERT INTO compactions({', '.join(cols)}) "
                  f"VALUES({', '.join('?' * len(cols))})", [full[k] for k in cols])
        v = as_view(full)
        seq = store.event(c, cid, CONTEXT_COMPACTED, {"compaction_id": comp_id, **v})
        return {**v, "seq": seq}

    return await store.write(fn)
