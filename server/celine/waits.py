"""Waiting for you: everything that waits on the user, in one list (GET /api/waits,
api/waits.py; the rail's Waiting and its sheet). The server's one answer; the client counts nothing.

- every request of the agent's (approvals.py), as its card;
- every mission in Needs you or with a message of its own that waits for the user, once,
  that message or its note as the line (missions/watch.py); a bug report it filed is
  an Intercom thread;
- every chat source whose login was lost and is not back (kb/connectors.py);
- every other Intercom thread with a message that waits for the user (intercom/store.py).

A freeze and a pause have their own rail items; runs, failed or waiting, belong to
Routines. `run` relays the events that change the list as one, `waits.changed`."""
from __future__ import annotations

import logging

import anyio

from .core.event_types import (
    APPROVALS_CHANGED,
    CONVERSATION_DELETED,
    CONVERSATION_RESTORED,
    INTERCOM_STATE,
    MISSION_STATE,
    SOURCE_STATE,
    WAITS_CHANGED,
)

log = logging.getLogger("celine.waits")

CHANGES = frozenset({APPROVALS_CHANGED, MISSION_STATE, SOURCE_STATE, INTERCOM_STATE,
                     CONVERSATION_DELETED, CONVERSATION_RESTORED})
# a lost login's line, by platform; any other platform reads LOST
LOST_BY = {"whatsapp": "Not connected to WhatsApp. Log in with QR code in Settings, WhatsApp.",
           "discord": "Not connected to Discord. Log in again in Settings, Discord."}
LOST = "Not connected to {title}. Log in again in Settings, {title}."
LINE = 320          # characters of a message on its item (the sheet shows three lines)


def _line(text: str | None) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= LINE else s[:LINE - 1].rstrip() + "…"


def lost_logins(connectors) -> list[dict]:
    """The chat sources whose login was there and got lost, and is not back."""
    if connectors is None:
        return []
    return [{"kind": "login", "id": s["kb"], "title": s["title"],
             "line": LOST_BY.get(s["platform"]) or LOST.format(title=s["title"]),
             "link": f"/settings/sources/{s['kb']}"}
            for s in connectors.states()
            if s.get("login_lost_at") and (s.get("connection") or {}).get("state") == "needs_login"]


def _missions(c) -> list[dict]:
    """Every mission in Needs you or with a message to the user that waits (its home is
    the mission: intercom/store.py MISSION_FROM), once; the line is the newest waiting
    message, else its note."""
    from . import homes
    from .core.ids import now
    from .intercom.store import _UNREAD, BUG
    rows = c.execute(
        "SELECT * FROM (SELECT m.conversation_id, m.name, m.note, m.status, m.updated_at, "
        "(SELECT i.text FROM intercom i WHERE i.from_addr = 'agent:' || m.conversation_id "
        f"AND i.kind <> 'job' AND {_UNREAD} AND NOT {BUG} ORDER BY i.id DESC LIMIT 1) AS asked "
        "FROM missions m JOIN conversations cv ON cv.id = m.conversation_id "
        "WHERE cv.trashed_at IS NULL) WHERE status = 'needs_you' OR asked IS NOT NULL "
        "ORDER BY updated_at DESC", ("user", now())).fetchall()
    return [{"kind": "mission", "id": r["conversation_id"], "title": r["name"],
             "line": _line(r["asked"] or r["note"]),
             "link": homes.of(c, r["conversation_id"])["link"]} for r in rows]


def _threads(c) -> list[dict]:
    from .intercom.store import thread_view, threads
    out = []
    for t in threads(c, "user", unread=True, received=True):
        v = thread_view(c, t)
        out.append({"kind": "thread", "id": str(t["thread"]),
                    "title": v["subject"] or v["name"], "line": _line(v["last"]["text"]),
                    "link": f"/intercom/{t['thread']}"})
    return out


# the head each kind is listed under in the sheet, the Notifications words
GROUPS = {"approval": "Approvals", "mission": "Missions", "login": "Sources",
          "thread": "Intercom"}


async def items(services) -> list[dict]:
    """Every item that waits, each with its `group` (GROUPS): the requests first (oldest
    first), then missions, lost logins and Intercom threads (newest first). A request is
    {kind: "approval", approval: its card}; every other {kind, id, title, line, link}."""
    a = getattr(services, "approvals", None)
    asks = [{"kind": "approval", "approval": card} for card in (a.view() if a else [])]
    rows = await services.store.read(lambda c: _missions(c) + _threads(c))
    missions = [r for r in rows if r["kind"] == "mission"]
    threads = [r for r in rows if r["kind"] == "thread"]
    out = [*asks, *missions, *lost_logins(getattr(services, "connectors", None)), *threads]
    return [{**i, "group": GROUPS[i["kind"]]} for i in out]


async def run(store, bus) -> None:
    """For as long as the app runs: each event that changes the list (CHANGES) on the
    global feed is followed by one `waits.changed`. The bus drops a slow reader, so this
    one subscribes again."""
    while True:
        tx, rx = bus.subscribe("global")
        try:
            async with rx:
                async for ev in rx:
                    if ev.get("type") in CHANGES:
                        store.emit_global({"type": WAITS_CHANGED})
        except anyio.get_cancelled_exc_class():
            raise
        except Exception:          # a bug: logged, the relay goes on
            log.exception("waits relay failed", extra={"event": "waits.relay_failed"})
        finally:
            bus.unsubscribe("global", tx)
