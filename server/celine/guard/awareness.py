"""What Celine knows about Bastet, as one line of her own mind (skills/mind.py), built
by the server from the tables: never a guess, never how Bastet decides.

- Frozen: since when, what for in plain words (bastet.plain), where (the
  conversation's title) and the judge's one sentence why, that it is her signal to wait for {{user}}, and whether Bastet
  asked {{user}} already (learn.py).
- Her thread with Bastet: the newest message Bastet wrote to her in this conversation
  within RECENT hours, so it answers in that thread (intercom reply_to).

Nothing while the guard is off (switch.py): her context is exactly as without Bastet.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from . import bastet, store as decisions
from .switch import enabled

RECENT = 24        # hours a message from Bastet counts as a thread she has with her


def _hm(t: str, tz) -> str:
    return f"{datetime.fromisoformat(t).astimezone(tz):%H:%M}"


def lines(c: sqlite3.Connection, cfg, tz, conversation_id: str, at: str) -> list[str]:
    """Her lines about Bastet now (`at`), oldest concern first; [] when there is none."""
    out = []
    f = decisions.open_freeze(c)
    if f is not None:
        d = decisions.row(c, f["decision_id"])
        what = bastet.plain(cfg, d["rule"], d["tool"]) if d else f["why"]
        title = (c.execute("SELECT title FROM conversations WHERE id=?",
                           (f["conversation_id"],)).fetchone() or [None])[0] \
            if f["conversation_id"] else None
        where = f' in "{title}"' if title else ""
        if d and (why := " ".join((d["why"] or "").split()).rstrip(".")):
            where += f". Why: {why}"
        line = bastet.fill(cfg, bastet.AWARE_FROZEN, at=_hm(f["at"], tz), what=what,
                           where=where)
        if f["asked_at"]:
            line += " " + bastet.fill(cfg, bastet.AWARE_ASKED, at=_hm(f["asked_at"], tz))
        out.append(line)
    since = (datetime.fromisoformat(at) - timedelta(hours=RECENT)).isoformat(
        timespec="milliseconds")
    if (m := decisions.last_to_her(c, conversation_id, since)) is not None:
        out.append(bastet.fill(cfg, bastet.AWARE_THREAD, at=_hm(m["created_at"], tz),
                               thread=m["thread"] or m["id"], id=m["id"]))
    return out


async def line(store, cfg, tz, conversation_id: str, at: str) -> str | None:
    """lines() as one line, None without any or while the guard is off."""
    if not enabled(cfg, conversation_id):
        return None
    got = await store.read(lambda c: lines(c, cfg, tz, conversation_id, at))
    return " ".join(got) or None


TOLD = "unfreeze_told"          # kv: the last freeze its mind told it about


async def unfrozen_line(store, cfg, tz) -> str | None:
    """The agent's own-mind line on its first turn after an unfreeze, once (skills/mind.py).
    Bastet's freeze in her words: the decision's plain what (bastet.plain), never the
    owner's sentence with its rule and evidence; their own freeze with their reason."""
    def fn(c: sqlite3.Connection) -> tuple[dict, sqlite3.Row | None] | None:
        r = c.execute("SELECT * FROM freezes WHERE unfrozen_at IS NOT NULL "
                      "ORDER BY id DESC LIMIT 1").fetchone()
        told = decisions.kv(c, TOLD)
        if r is None or (told and int(told) >= r["id"]):
            return None
        decisions.kv_set(c, TOLD, str(r["id"]))
        return dict(r), decisions.row(c, r["decision_id"])
    got = await store.write(fn)
    if got is None:
        return None
    r, d = got
    if r["by"] == "guard":
        return bastet.fill(cfg, bastet.LIFTED if r["unfrozen_by"] == "bastet"
                           else bastet.UNFROZEN, at=_hm(r["at"], tz),
                           back=_hm(r["unfrozen_at"], tz),
                           what=bastet.plain(cfg, d["rule"] if d else None,
                                             d["tool"] if d else None).rstrip("."))
    return bastet.fill(cfg, bastet.UNFROZEN_BY_USER, at=_hm(r["at"], tz),
                       why=r["why"].rstrip("."), back=_hm(r["unfrozen_at"], tz))
