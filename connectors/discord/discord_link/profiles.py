"""Profiles of everyone the connector has seen: bio, pronouns, whether the account exists.

One request at a time, far apart. The first pass reads a person never read before every
FIRST_SECONDS; after it the profile read longest ago is read again every REFRESH_SECONDS,
so every profile comes round again and a change is journaled as a person record (the
archive keeps the old value in the person's name history). Names also change with every
message a person writes: its author is journaled with it.

Discord answers 404 for an account that no longer exists: journaled as deleted, with no
names, so the archive keeps the last ones. 403: the profile is not shown to this account;
nothing is journaled.
"""
from __future__ import annotations

import logging
from typing import Any

from .records import person
from .rest import Rest
from .state import State, now_ms

log = logging.getLogger("discord_link.profiles")

FIRST_SECONDS = 120.0
REFRESH_SECONDS = 900.0


def record(uid: str, body: dict[str, Any] | None) -> dict[str, Any]:
    """The person record of one profile answer; None: the account is gone."""
    if body is None:
        return {"key": uid, "deleted": True}
    prof = body.get("user_profile") or {}
    user = body.get("user") or {}
    p = person({**user, "id": uid}) or {"key": uid}
    if p.get("deleted"):
        return p
    # an empty bio is a fact too: the archive ends the one it had
    return {**p, "bio": prof.get("bio") or user.get("bio") or "",
            "pronouns": prof.get("pronouns") or "", "deleted": False, "profile": True}


def due(state: State, now: int | None = None) -> str | None:
    """The user whose profile is read now, or None: a never-read one FIRST_SECONDS after
    the last read, else the one read longest ago, REFRESH_SECONDS after the last read."""
    now = now_ms() if now is None else now
    last = state.get("profile_read_at") or 0
    row = state.one("SELECT user_id, fetched_at FROM profiles "
                    "ORDER BY fetched_at IS NOT NULL, fetched_at LIMIT 1")
    if row is None:
        return None
    gap = FIRST_SECONDS if row["fetched_at"] is None else REFRESH_SECONDS
    return row["user_id"] if now - last >= gap * 1000 else None


def step(rest: Rest, state: State) -> str | None:
    """Read the one profile that is due and journal it. -> its user id."""
    uid = due(state)
    if uid is None:
        return None
    state.set("profile_read_at", now_ms())        # before the request: a failure waits too
    try:
        body = rest.get(f"/users/{uid}/profile", {"with_mutual_guilds": "false",
                                                  "with_mutual_friends": "false"})
        rec = record(uid, body or {})
    except FileNotFoundError:
        rec = record(uid, None)
    except PermissionError:
        rec = None
    if rec is not None:
        state.journal([{"kind": "person", "person": rec}])
    state.x("UPDATE profiles SET fetched_at = ? WHERE user_id = ?", (now_ms(), uid))
    return uid
