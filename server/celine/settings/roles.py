"""Which model role a conversation's turns use: a run's role is its schedule's
(schedules/runs.py), a chat's `chat`, any other kind's `tasks`. A conversation's own
model still goes first (Settings.chat_chain). `load_all` fills CONVERSATION_ROLES at
startup, so they count before a conversation's first turn.
"""
from __future__ import annotations

from . import CONVERSATION_ROLES


# a run's conversation (trigger_key `run:<run id>:<try>`) and the schedule of its run
_SELECT = ("SELECT c.id, c.kind, r.schedule FROM conversations c "
           "LEFT JOIN runs r ON c.trigger_key LIKE 'run:%' "
           "AND r.id = substr(c.trigger_key, 5, 32)")


def role_of(kind: str) -> str:
    """A chat runs on the chat role, every other kind (a mission, a sub-agent, a
    background run) on tasks."""
    return "chat" if kind == "chat" else "tasks"


def _keep(cfg, cid: str, kind: str, schedule: str | None = None) -> str:
    run_of = (getattr(cfg, "schedules", None) or {}).get(schedule or "")
    CONVERSATION_ROLES[cid] = run_of.role if run_of else role_of(kind)
    return CONVERSATION_ROLES[cid]


async def resolve(store, cfg, cid: str) -> str:
    """The conversation's role, also kept in CONVERSATION_ROLES for chat_chain."""
    row = await store.read(lambda c: c.execute(f"{_SELECT} WHERE c.id=?",
                                               (cid,)).fetchone())
    return _keep(cfg, cid, row["kind"] if row else "chat", row["schedule"] if row else None)


async def load_all(store, cfg) -> int:
    """Every conversation's role, at startup."""
    rows = await store.read(lambda c: [tuple(r) for r in c.execute(_SELECT)])
    CONVERSATION_ROLES.clear()
    for cid, kind, schedule in rows:
        _keep(cfg, cid, kind, schedule)
    return len(rows)
