"""What changed for a client coming back (GET /api/changes, api/ws.py): the global feed's
bounds, and since a global id, the conversations, jobs, unread messages and sub-agents
it needs again."""
from __future__ import annotations

import json
import sqlite3

from ..core.event_types import CONVERSATION_ARCHIVED, CONVERSATION_UNARCHIVED, JOB_STATE
from ..core.states import LIVE_SQL
from .store import family


def bounds(c: sqlite3.Connection) -> tuple[int | None, int]:
    lo, hi = c.execute("SELECT min(id), max(id) FROM global_events").fetchone()
    return lo, hi or 0


def behind(lo: int | None, after: int) -> bool:
    """`after` is below what the feed keeps: rows after it were pruned."""
    return lo is not None and after < lo - 1


def catch_up(c: sqlite3.Connection, since: int | None, known: dict[str, int]) -> dict:
    """`known`: {conversation id: the seq the client has}."""
    from ..intercom.store import waiting
    from ..sub_agents.skill import agents_of
    lo, hi = bounds(c)
    stale = since is not None and behind(lo, since)
    named: set[str] = set()        # conversations the feed names after `since`
    job_ids: set[str] = set()
    if since is not None and not stale:
        for r in c.execute("SELECT type, data FROM global_events WHERE id > ?",
                           (since,)):
            data = json.loads(r["data"])
            if r["type"] == JOB_STATE:
                job_ids.add(str(data.get("job_id")))
            if r["type"] in (CONVERSATION_ARCHIVED, CONVERSATION_UNARCHIVED):
                named.update(family(c, data["id"]))     # the sub-agents go with it
            elif r["type"].startswith("conversation.") and data.get("id"):
                named.add(data["id"])
            elif data.get("conversation"):
                named.add(data["conversation"])
    moved, gone = [], []
    for cid in [*known, *sorted(named - set(known))]:
        r = c.execute(
            "SELECT c.id, c.next_seq - 1 AS seq, c.title, c.archived_at, "
            "(SELECT state FROM turns t WHERE t.conversation_id = c.id AND t.state IN "
            f"{LIVE_SQL} ORDER BY t.created_at LIMIT 1) AS active_state "
            "FROM conversations c WHERE c.id=? AND c.trashed_at IS NULL", (cid,)).fetchone()
        if r is None:
            gone.append(cid)
        elif cid in named or r["seq"] > known[cid]:
            moved.append(dict(r))
    marks = ",".join("?" * len(job_ids))
    jobs = [dict(r) for r in c.execute(
        "SELECT id, conversation_id, tool_name AS tool, state, finished_at FROM jobs "
        f"WHERE id IN ({marks}) AND finished_at IS NOT NULL ORDER BY finished_at",
        sorted(job_ids))] if job_ids else []
    return {"global": hi, "resync": stale, "conversations": moved, "gone": gone,
            "jobs": jobs, "unread": waiting(c, "user"), "agents": agents_of(c)}
