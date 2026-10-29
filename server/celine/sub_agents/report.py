"""A sub-agent's report to the conversation that started it.

A sub-agent is done when its conversation is quiet: no live turn, no running job,
nothing waiting to be delivered that would wake it (a message it scheduled to itself
included), and every sub-agent of its own done. That is checked at every end of its
turns and jobs and of its sub-agents' (the dispatcher's `on_end`). A report goes only
for work it was given: a stretch with a brief or a message from its parent or the user
since its last report; a message from its own sub-agent alone gets no report. It goes
to the one that started it, never further up. The report is a head (its name, how its
last turn ended, what its work since its last report took: the models that answered,
the tokens in and out, the seconds its turns ran, the notes it changed) and its final
answer (not what it said between tool calls), cut to REPORT_SHARE of the parent's
window; the rest stays in its conversation. How its last turn ended is in
`data.state`, the work in `data.work`. A job's result therefore reaches the report,
never a placeholder."""
from __future__ import annotations

import sqlite3

from .. import metrics
from ..conversations.tree import final_words
from ..core.states import LIVE_SQL
from ..guard.sources import agent_read_outside
from ..intercom.store import name_of
from ..turns import tokens

REPORT_SHARE = 0.03     # of the parent's window, about 2k tokens of a 65k one
CUT = "[cut: the rest is in its conversation]"
CHANGES = ("notebook__write", "notebook__edit", "notebook__delete", "notebook__move")
FILES_SHOWN = 10        # notes named in a report's head, the rest a count


def quiet(c: sqlite3.Connection, cid: str, but: int | None = None) -> bool:
    """No live turn, running job or waking message waiting in `cid`, and every sub-agent
    of it quiet and not queued; `but` is a message in `cid` that does not count (a
    mission's time limit)."""
    if c.execute(
        f"SELECT 1 FROM turns WHERE conversation_id=? AND state IN {LIVE_SQL} "
        "UNION ALL SELECT 1 FROM jobs WHERE conversation_id=? AND state='running' "
        "UNION ALL SELECT 1 FROM intercom_deliveries d JOIN intercom i ON i.id = "
        "d.intercom_id WHERE d.conversation_id=? AND d.state='pending' AND i.wake=1 "
        "AND i.cancelled_at IS NULL AND i.id IS NOT ? LIMIT 1",
        (cid, cid, cid, but)).fetchone():
        return False
    from .skill import waiting
    return all(waiting(c, s) is None and quiet(c, s) for (s,) in c.execute(
        "SELECT id FROM conversations WHERE agent_of=?", (cid,)).fetchall())


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
    return r["content"] if r else ""


def work(c: sqlite3.Connection, cid: str, whole: bool = False) -> dict:
    """What the turns of `cid` since its last report (`whole`: ever) took: {models: the
    models that answered, the newest last; prompt_tokens, completion_tokens: summed as
    the servers counted them; seconds: the turns' run time, summed; files: the notes
    its calls changed, oldest first}."""
    last = "" if whole else c.execute(
        "SELECT coalesce(max(created_at), '') FROM intercom WHERE from_addr=? AND "
        "kind='report'", (f"agent:{cid}",)).fetchone()[0]
    turns = "SELECT id FROM turns WHERE conversation_id=? AND created_at > ?"
    rows = c.execute(
        f"SELECT provider, coalesce(sum(prompt_tokens), 0) p, "
        f"coalesce(sum(completion_tokens), 0) o, max(created_at) at FROM requests "
        f"WHERE turn_id IN ({turns}) GROUP BY provider ORDER BY at", (cid, last)).fetchall()
    secs = c.execute(
        "SELECT coalesce(sum(julianday(finished_at) - julianday(created_at)), 0) * 86400 "
        "FROM turns WHERE conversation_id=? AND created_at > ? AND finished_at IS NOT NULL",
        (cid, last)).fetchone()[0]
    marks = ",".join("?" * len(CHANGES))
    files = [r[0] for r in c.execute(
        f"SELECT coalesce(json_extract(tc.arguments, '$.dst'), "
        f"json_extract(tc.arguments, '$.path')) AS f, min(tc.started_at) AS at FROM "
        f"tool_calls tc JOIN messages m ON m.id = tc.message_id WHERE "
        f"m.conversation_id=? AND tc.state='ok' AND tc.name IN ({marks}) AND "
        f"tc.started_at > ? GROUP BY f ORDER BY at", (cid, *CHANGES, last)) if r[0]]
    return {"models": [r["provider"] for r in rows],
            "prompt_tokens": sum(r["p"] for r in rows),
            "completion_tokens": sum(r["o"] for r in rows), "seconds": round(secs),
            "files": files}


def head(name: str, state: str, w: dict) -> str:
    """"[scout, done, on llama/qwen: 12,345 tokens in, 678 out, 42 s; changed a.md]"."""
    bits = [name or "sub-agent", state]
    if w["models"]:
        bits.append(f"on {', '.join(w['models'])}: {w['prompt_tokens']:,} tokens in, "
                    f"{w['completion_tokens']:,} out, {w['seconds']} s")
    out = ", ".join(bits)
    if files := w["files"]:
        more = len(files) - FILES_SHOWN
        out += "; changed " + ", ".join(files[:FILES_SHOWN]) + (
            f" and {more} more" if more > 0 else "")
    return f"[{out}]"


def window_of(c: sqlite3.Connection, cid: str) -> int:
    """The window of the model that last answered in `cid`, as its request stored it."""
    r = c.execute("SELECT r.window FROM requests r JOIN turns t ON t.id = r.turn_id "
                  "WHERE t.conversation_id=? AND r.window IS NOT NULL "
                  "ORDER BY r.created_at DESC LIMIT 1", (cid,)).fetchone()
    return r[0] if r else tokens.FALLBACK_WINDOW


def cap(c: sqlite3.Connection, parent: str) -> int:
    """The most of a report's answer that goes up to `parent`, in tokens."""
    return int(window_of(c, parent) * REPORT_SHARE)


def _facts(c: sqlite3.Connection, cid: str) -> dict | None:
    sub = c.execute("SELECT agent_of FROM conversations WHERE id=?", (cid,)).fetchone()
    if sub is None or not sub["agent_of"]:
        return None
    parent = sub["agent_of"]
    reply_to = asked(c, cid, parent)
    if reply_to is None or not quiet(c, cid):
        return {"reply_to": None}           # nothing asked, or not done yet
    turn = c.execute("SELECT state FROM turns WHERE conversation_id=? "
                     "ORDER BY created_at DESC LIMIT 1", (cid,)).fetchone()
    job = c.execute("SELECT state FROM jobs WHERE conversation_id=? "
                    "ORDER BY created_at DESC LIMIT 1", (cid,)).fetchone()
    state = turn["state"] if turn else "done"
    # stopped: its last turn was aborted, or its last job cancelled (a Stop that came
    # after the turn had ended). Such a report never wakes the parent.
    stopped = state == "aborted" or bool(job and job["state"] == "cancelled")
    return {"parent": parent, "text": last_answer(c, cid), "state": state,
            "stopped": stopped, "reply_to": reply_to, "work": work(c, cid),
            "outside": agent_read_outside(c, cid), "name": name_of(c, f"agent:{cid}"),
            "cap": cap(c, parent)}


async def on_end(services, cid: str) -> None:
    """When sub-agent `cid` is done (above) with work it was given, its one report. Every
    end of a sub-agent's turn or job also announces its state (skill.announce). What it
    left open closes in leftovers.py."""
    await _report(services, cid)
    # its parent, a sub-agent too, may have waited on it alone
    up = await services.store.read(lambda c: c.execute(
        "SELECT agent_of FROM conversations WHERE id=?", (cid,)).fetchone())
    if up is not None and up[0]:
        await _report(services, up[0])
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
    text = f["text"] or "(it wrote nothing)"
    shown_text, shown, total = tokens.cut_to(text, f["cap"])
    if shown < total:
        text = f"{shown_text}\n\n{CUT}"
    text = f"{head(f['name'], f['state'], f['work'])}\n\n{text}"
    # a sub-agent that was stopped reports without waking its parent: the parent
    # stopped it (its Stop cascades) or knows, and a Stop must stick
    sent = await services.intercom.send(
        f"agent:{cid}", f"agent:{f['parent']}", text, kind="report",
        reply_to=f["reply_to"], data={"state": f["state"], "work": f["work"], **({"outside": {
            "source": f"sub-agent {f['name'] or ''}".strip(), "by": None}}
            if f["outside"] else {})}, wake=not f["stopped"],
        only_if=lambda c: quiet(c, cid) and asked(c, cid, f["parent"]) == f["reply_to"])
    if sent is not None:
        metrics.SUB_AGENTS_FINISHED.labels(f["state"]).inc()
