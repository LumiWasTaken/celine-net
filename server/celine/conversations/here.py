"""Where it works: the conversation this turn runs in (a chat, a mission, a Routine's
run, a sub-agent's brief, a background task) with its intercom address, in its system
prompt (it does not change), and the other conversations of its own working right now, in
the tail of a turn when there are any (turns/prompt.py). So it knows which of its chats
it is in and where else it is busy, and can write there."""
from __future__ import annotations

import sqlite3

from ..core.states import LIVE_SQL

OTHERS_MAX = 6


def _what(c: sqlite3.Connection, r) -> str:
    """One conversation in its words, with its address."""
    title = f"\"{r['title']}\"" if r["title"] else "untitled"
    key = r["trigger_key"] or ""
    if key.startswith("run:"):
        run = c.execute("SELECT id, label FROM runs WHERE conversation_id=? ORDER BY slot "
                        "DESC LIMIT 1", (r["id"],)).fetchone()
        if run is not None:
            return f"the run of the Routine {run['label']} (address run:{run['id']})"
    if r["kind"] == "mission":
        m = c.execute("SELECT name FROM missions WHERE conversation_id=?",
                      (r["id"],)).fetchone()
        return f"the mission {m['name'] if m else title} (address {r['id']})"
    if r["kind"] == "agent":
        return f"the sub-agent {r['agent_name'] or title} (address {r['id']})"
    if key.startswith("bridge:"):
        return f"the bridged chat {title} (address {r['id']})"
    if r["kind"] == "chat":
        return f"the chat {title} (address {r['id']})"
    return f"the background task {title} (address {r['id']})"


COLS = "id, kind, title, trigger_key, agent_name, agent_of"


def where(c: sqlite3.Connection, cid: str) -> str | None:
    """Where it works, None for a conversation that is gone."""
    me = c.execute(f"SELECT {COLS} FROM conversations WHERE id=?", (cid,)).fetchone()
    if me is None:
        return None
    out = [f"You are working in {_what(c, me)}."]
    if me["agent_of"]:
        boss = c.execute(f"SELECT {COLS} FROM conversations WHERE id=?",
                         (me["agent_of"],)).fetchone()
        if boss is not None:
            out.append(f"It reports to {_what(c, boss)}.")
    return " ".join(out)


def busy(c: sqlite3.Connection, cid: str) -> str | None:
    """The other conversations working now, None when none is."""
    others = c.execute(
        f"SELECT DISTINCT c.id, c.kind, c.title, c.trigger_key, c.agent_name, c.agent_of "
        f"FROM turns t JOIN conversations c ON c.id = t.conversation_id "
        f"WHERE t.state IN {LIVE_SQL} AND c.id != ? AND c.trashed_at IS NULL "
        f"ORDER BY t.created_at LIMIT ?", (cid, OTHERS_MAX)).fetchall()
    return "[also working now] " + "; ".join(_what(c, o) for o in others) + "." \
        if others else None
