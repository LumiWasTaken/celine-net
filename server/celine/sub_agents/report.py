"""A sub-agent's report to the conversation that started it.

A sub-agent is done when its conversation is quiet: no live turn, no running job, and
nothing waiting to be delivered that would wake it (a message it scheduled to itself
included). That is checked at every end of its turns and jobs (the dispatcher's
`on_end`). A report goes only for work it was given: a stretch with a brief or a message
from its parent or the user since its last report. A sibling's message alone gets no
report to the parent. The report is its final answer (not what it said between tool
calls), capped like a tool result, without raw web citations, with how its last turn
ended in `data.state`. A job's result therefore reaches the report, never a
placeholder."""
from __future__ import annotations

import sqlite3

from .. import metrics
from ..conversations.tree import final_words
from ..core.states import LIVE_SQL
from ..guard.sources import agent_read_outside
from ..intercom.store import name_of
from ..skills.web import uncite
from ..turns import tokens


def quiet(c: sqlite3.Connection, cid: str, but: int | None = None) -> bool:
    """No live turn, running job or waking message waiting in `cid`; `but` is a message
    that does not count (a mission's time limit)."""
    return not c.execute(
        f"SELECT 1 FROM turns WHERE conversation_id=? AND state IN {LIVE_SQL} "
        "UNION ALL SELECT 1 FROM jobs WHERE conversation_id=? AND state='running' "
        "UNION ALL SELECT 1 FROM intercom_deliveries d JOIN intercom i ON i.id = "
        "d.intercom_id WHERE d.conversation_id=? AND d.state='pending' AND i.wake=1 "
        "AND i.cancelled_at IS NULL AND i.id IS NOT ? LIMIT 1",
        (cid, cid, cid, but)).fetchone()


def asked(c: sqlite3.Connection, cid: str, parent: str) -> int | None:
    """The latest brief or message from the parent or the user that landed in `cid`
    since its last report, or None: nothing since then is theirs."""
    last = c.execute("SELECT coalesce(max(id), 0) FROM intercom WHERE from_addr=? AND "
                     "kind='report'", (f"agent:{cid}",)).fetchone()[0]
    r = c.execute(
        "SELECT max(i.id) FROM intercom i JOIN intercom_deliveries d ON d.intercom_id = "
        "i.id WHERE d.conversation_id=? AND d.state <> 'pending' AND i.id > ? AND "
        "i.kind IN ('brief', 'message') AND i.from_addr IN (?, 'user')",
        (cid, last, f"agent:{parent}")).fetchone()
    return r[0]


def last_answer(c: sqlite3.Connection, cid: str) -> str:
    """Its last answer: the final words of its latest turn (conversations/tree.py), else the
    newest reply that said anything."""
    t = c.execute("SELECT id FROM turns WHERE conversation_id=? ORDER BY created_at DESC "
                  "LIMIT 1", (cid,)).fetchone()
    if t is not None and (words := final_words(c, t["id"])):
        return words
    r = c.execute("SELECT content FROM messages WHERE conversation_id=? AND "
                  "role='assistant' AND coalesce(content, '') <> '' ORDER BY created_at "
                  "DESC, rowid DESC LIMIT 1", (cid,)).fetchone()
    return uncite(r["content"]).strip() if r else ""


def _facts(c: sqlite3.Connection, cid: str) -> dict | None:
    sub = c.execute("SELECT agent_of FROM conversations WHERE id=?", (cid,)).fetchone()
    if sub is None or not sub["agent_of"]:
        return None
    parent = sub["agent_of"]
    reply_to = asked(c, cid, parent)
    if reply_to is None:
        return {"reply_to": None}
    turn = c.execute("SELECT state FROM turns WHERE conversation_id=? "
                     "ORDER BY created_at DESC LIMIT 1", (cid,)).fetchone()
    job = c.execute("SELECT state FROM jobs WHERE conversation_id=? "
                    "ORDER BY created_at DESC LIMIT 1", (cid,)).fetchone()
    state = turn["state"] if turn else "done"
    # stopped: its last turn was aborted, or its last job cancelled (a Stop that came
    # after the turn had ended). Such a report never wakes the parent.
    stopped = state == "aborted" or bool(job and job["state"] == "cancelled")
    return {"parent": parent, "text": last_answer(c, cid), "state": state,
            "stopped": stopped, "reply_to": reply_to,
            "outside": agent_read_outside(c, cid), "name": name_of(c, f"agent:{cid}")}


async def on_end(services, cid: str) -> None:
    """When sub-agent `cid` is done (above) with work it was given, its one report. Every
    end of a sub-agent's turn or job also announces its state (skill.announce). What it
    left open closes in leftovers.py."""
    await _report(services, cid)
    from .skill import queued_sql, release
    # a queued sibling (skill.release) starts when this one is done
    row = await services.store.read(lambda c: c.execute(
        f"SELECT s.agent_of FROM conversations s WHERE s.id=? AND EXISTS (SELECT 1 FROM "
        f"conversations o JOIN intercom_deliveries d ON d.conversation_id = o.id JOIN "
        f"intercom i ON i.id = d.intercom_id WHERE o.agent_of = s.agent_of AND "
        f"{queued_sql('o.id')})",
        (cid,)).fetchone())
    if row is not None:
        await release(services, row["agent_of"])


async def _report(services, cid: str) -> None:
    store = services.store
    f = await store.read(lambda c: _facts(c, cid))
    if f is None:
        return
    from .skill import announce
    await announce(services, cid)
    if f["reply_to"] is None:
        return
    metrics.SUB_AGENTS_FINISHED.labels(f["state"]).inc()
    text = f["text"] or "(it wrote nothing)"
    head, shown, total = tokens.cut_to(text, tokens.result_cap(tokens.FALLBACK_WINDOW))
    if shown < total:
        text = f"{head}\n\n[cut: the whole report is in its conversation.]"
    # a sub-agent that was stopped reports without waking its parent: the parent
    # stopped it (its Stop cascades) or knows, and a Stop must stick
    await services.intercom.send(
        f"agent:{cid}", f"agent:{f['parent']}", text, kind="report",
        reply_to=f["reply_to"], data={"state": f["state"], **({"outside": {
            "source": f"sub-agent {f['name'] or ''}".strip(), "by": None}}
            if f["outside"] else {})}, wake=not f["stopped"],
        only_if=lambda c: quiet(c, cid) and asked(c, cid, f["parent"]) == f["reply_to"])
