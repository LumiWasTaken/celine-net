"""The context block of a user message: what skills add to the end of it on the wire.

It is computed once, when the message is first sent (a turn starts at it, or a turn
takes it at a step boundary), and stored in `message_context`; so is the tail of the
turn that answers it (put_tail, turns/prompt.py tail_parts), sent after the block. Every
later request reads the stored text, so the history it reads stays byte-stable for the
KV cache even when what the skill would compute has changed since. A message no skill adds
anything to stores an empty row, so a resent message is never asked again and its
wire never gains text later.
"""
from __future__ import annotations

import json
import sqlite3

from ..skills import ToolContext


async def ensure(store, cfg, registry, cid: str, tid: str, msgs: list[dict],
                 budget: int | None = None) -> None:
    """Computes and stores the block of each user message in `msgs` that has none stored yet,
    and sets it as `m["context"]` on the dicts. `budget`: the most tokens one block may
    take (ToolContext.max_output_tokens)."""
    if registry is None:
        return
    ids = [m["id"] for m in msgs if m["role"] == "user"]
    marks = ",".join("?" * len(ids))
    have = {r[0] for r in await store.read(lambda c: c.execute(
        f"SELECT message_id FROM message_context WHERE message_id IN ({marks}) "
        f"AND text IS NOT NULL",
        ids).fetchall())}
    users = [m for m in msgs if m["role"] == "user" and m["id"] not in have]
    if not users:
        return
    ctx = ToolContext(conversation_id=cid, turn_id=tid, tool_call_id="",
                      store=store, config=cfg, emit=None,  # type: ignore[arg-type]
                      max_output_tokens=budget)
    for m in users:
        text, report = await registry.message_context(ctx, m) or ("", {})
        def put(c, m=m, text=text, report=report):
            c.execute("INSERT INTO message_context(message_id, text, report) VALUES(?,?,?) "
                      "ON CONFLICT(message_id) DO UPDATE SET text=excluded.text, "
                      "report=excluded.report WHERE message_context.text IS NULL",
                      (m["id"], text, json.dumps(report, ensure_ascii=False)))
        await store.write(put)
        m["context"] = text


async def tail_of(store, chain: list[dict]) -> tuple[str | None, str | None]:
    """(the newest user message on `chain`, the tail stored on it, None when none is)."""
    mid = next((m["id"] for m in reversed(chain) if m["role"] == "user"), None)
    if mid is None:
        return None, None
    r = await store.read(lambda c: c.execute(
        "SELECT tail FROM message_context WHERE message_id=?", (mid,)).fetchone())
    return mid, r[0] if r else None


async def put_tail(store, mid: str, parts: list[list], fresh: bool = False) -> None:
    """The tail of the turn that answers user message `mid` (turns/prompt.py tail_parts,
    stored as JSON: [[key, value], ...]), written once: a message with a tail keeps it
    (a later step, a Continue or a restart sends the same bytes).
    `fresh`: a new answer to it (Regenerate) writes its own; the reply before it is done,
    so nothing reads the old one again."""
    await store.write(lambda c: c.execute(
        "INSERT INTO message_context(message_id, tail) VALUES(?, ?) "
        "ON CONFLICT(message_id) DO UPDATE SET tail=excluded.tail "
        "WHERE message_context.tail IS NULL OR ?",
        (mid, json.dumps(parts, ensure_ascii=False), fresh)))


def unsent(chain: list[dict]) -> list[dict]:
    """The user messages after the newest reply: the ones a starting turn sends first."""
    out: list[dict] = []
    for m in reversed(chain):
        if m["role"] != "user":
            break
        out.append(m)
    return out[::-1]


def reports(c: sqlite3.Connection, cid: str, mid: str,
            path: str) -> list[tuple[str, str, object]]:
    """The messages before `mid` on its branch that it still sees, newest first, back
    to the newest compaction: (role, content, the value at JSON `path` of its block's
    report, None without one)."""
    throughs = {r[0] for r in c.execute(
        "SELECT through_message_id FROM compactions WHERE conversation_id=?", (cid,))}
    out: list[tuple[str, str, object]] = []
    pid = (c.execute("SELECT parent_id FROM messages WHERE id=?", (mid,)).fetchone()
           or [None])[0]
    while pid and pid not in throughs:
        r = c.execute("SELECT m.parent_id, m.role, m.content, x.report -> ? "
                      "FROM messages m LEFT JOIN message_context x ON x.message_id = m.id "
                      "WHERE m.id=?", (path, pid)).fetchone()
        if r is None:
            break
        out.append((r[1], r[2] or "", json.loads(r[3]) if r[3] is not None else None))
        pid = r[0]
    return out


def last_report(c: sqlite3.Connection, cid: str, mid: str, path: str):
    """The newest value at JSON `path` of a block it still sees before `mid`
    (reports), None when there is none."""
    return next((v for _, _, v in reports(c, cid, mid, path) if v is not None),
                None)
