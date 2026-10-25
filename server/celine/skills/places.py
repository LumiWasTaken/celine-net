"""Where a turn runs, and which skills it is offered there.

A place is one of PLACE_NAMES: `chat` (its chats), `mission` (a mission) and
`background` (a trigger's or a schedule's run). A
sub-agent works in its root's place.

`PLACES` maps a skill to the places it is offered in; a skill it does not name (an MCP
connector) is offered in CONNECTOR_PLACES. `offered(skill, place)` says it in one call.
"""
from __future__ import annotations

import sqlite3

PLACE_NAMES = ("chat", "mission", "background")
_WORK = frozenset(PLACE_NAMES)
CONNECTOR_PLACES = _WORK

PLACES: dict[str, frozenset[str]] = {
    # core
    "skills": _WORK,
    "notebook": _WORK,
    "intercom": _WORK,
    "past_conversations": _WORK,
    "sub_agents": _WORK,
    "mind": _WORK,
    "plan": _WORK,
    "bugreport": _WORK,
    # its tools
    "shell": _WORK,
    "code": _WORK,
    "chat_archive": _WORK,
    "user_documents": _WORK,
    "email": _WORK,
    "vault": _WORK,
    "artifacts": _WORK,
    "music_generator": _WORK,
    "writer": _WORK,
    "models": _WORK,
    "web": _WORK,
    "browser": _WORK,
    "image_generator": _WORK,
    "appearance": frozenset({"chat"}),
    "routines": _WORK,
    "me": _WORK,
    # missions__list everywhere, the rest in a mission (Missions.offered_in)
    "missions": _WORK,
}

_KIND_PLACE = {"chat": "chat", "mission": "mission", "background": "background"}


def offered(skill: str, place: str) -> bool:
    """Whether `skill` is offered to a turn in `place`."""
    return place in PLACES.get(skill, CONNECTOR_PLACES)


def place_of(c: sqlite3.Connection, cid: str, turn_id: str | None = None) -> str:
    """The place of conversation `cid`: chat, mission or background. A sub-agent takes
    its root's place (one whose root is gone: background); a trigger's or a schedule's
    conversation is background."""
    seen: set[str] = set()
    row = None
    while cid and cid not in seen:
        seen.add(cid)
        row = c.execute("SELECT id, kind, agent_of, trigger_key FROM conversations "
                        "WHERE id=?", (cid,)).fetchone()
        if row is None or row[1] != "agent":
            break
        cid = row[2]
    if row is None or row[1] == "agent":
        return "background"
    key = row[3] or ""
    if key.startswith(("trigger:", "run:")):
        return "background"
    return _KIND_PLACE.get(row[1], "background")
