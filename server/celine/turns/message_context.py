"""The context block of a user message: what skills add to the end of it on the wire.

It is computed once, when the message is first sent (a turn starts at it, or a turn
takes it at a step boundary), and stored in `message_context`; so is the tail of the
turn that answers it (put_tail, turns/prompt.py tail_text), sent after the block. Every
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
        f"SELECT message_id FROM message_context WHERE message_id IN ({marks})",
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
            c.execute("INSERT OR IGNORE INTO message_context(message_id, text, report) "
                      "VALUES(?,?,?)", (m["id"], text, json.dumps(report, ensure_ascii=False)))
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


async def put_tail(store, mid: str, tail: str) -> None:
    """The tail of the turn that answers user message `mid`, written once: a message
    with a tail keeps it (a later turn on it sends the same bytes)."""
    await store.write(lambda c: c.execute(
        "INSERT INTO message_context(message_id, text, tail) VALUES(?, '', ?) "
        "ON CONFLICT(message_id) DO UPDATE SET tail=excluded.tail "
        "WHERE message_context.tail IS NULL", (mid, tail)))


def unsent(chain: list[dict]) -> list[dict]:
    """The user messages after the newest reply: the ones a starting turn sends first."""
    out: list[dict] = []
    for m in reversed(chain):
        if m["role"] != "user":
            break
        out.append(m)
    return out[::-1]


def history_limit(cfg) -> int | None:
    """How many earlier messages a request carries (`history_messages`); None: all."""
    return int(cfg.r("history_messages") or 0) or None


def reports(c: sqlite3.Connection, cid: str, mid: str, path: str,
            limit: int | None) -> list[tuple[str, str, object]]:
    """The messages before `mid` on its branch that it still sees, newest first, back
    to the newest compaction or `limit` messages (tool results not counted): (role,
    content, the value at JSON `path` of its block's report, None without one).
    note: the limit counts every message but tool results, so it errs toward one
    more message; context._Plan._cut is the exact cut, made at request time."""
    throughs = {r[0] for r in c.execute(
        "SELECT through_message_id FROM compactions WHERE conversation_id=?", (cid,))}
    out: list[tuple[str, str, object]] = []
    seen = 0
    pid = (c.execute("SELECT parent_id FROM messages WHERE id=?", (mid,)).fetchone()
           or [None])[0]
    while pid and pid not in throughs and (limit is None or seen < limit):
        r = c.execute("SELECT m.parent_id, m.role, m.content, x.report -> ? "
                      "FROM messages m LEFT JOIN message_context x ON x.message_id = m.id "
                      "WHERE m.id=?", (path, pid)).fetchone()
        if r is None:
            break
        out.append((r[1], r[2] or "", json.loads(r[3]) if r[3] is not None else None))
        pid, seen = r[0], seen + (r[1] != "tool")
    return out


def last_report(c: sqlite3.Connection, cid: str, mid: str, path: str,
                limit: int | None):
    """The newest value at JSON `path` of a block it still sees before `mid`
    (reports), None when there is none."""
    return next((v for _, _, v in reports(c, cid, mid, path, limit) if v is not None),
                None)
