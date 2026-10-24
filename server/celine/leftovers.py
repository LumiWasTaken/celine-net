"""What a conversation leaves open: when a mission is done or stopped,
or a sub-agent's conversation is done (quiet: sub_agents/report.quiet), the terminals it
opened close, whatever runs in them, their history kept (terminals/sync.py), and so do
its browser tabs (browser/tabs.py). Checked at every end of a turn or job (the
dispatcher's on_end) and when the user stops a mission."""
from __future__ import annotations

import sqlite3

from .browser import tabs
from .sub_agents.report import quiet


def done(c: sqlite3.Connection, cid: str) -> bool:
    r = c.execute("SELECT c.agent_of, mi.status FROM conversations c LEFT JOIN missions mi "
                  "ON mi.conversation_id = c.id WHERE c.id=?", (cid,)).fetchone()
    if r is None:
        return False
    if r["status"] is not None:
        return r["status"] in ("done", "stopped") and quiet(c, cid)
    return r["agent_of"] is not None and quiet(c, cid)


async def on_end(services, cid: str) -> None:
    if not await services.store.read(lambda c: done(c, cid)):
        return
    if services.terminals is not None:
        await services.terminals.close_opened_by(cid)
    await tabs.close_opened_by(services.cfg, services.store, cid)
