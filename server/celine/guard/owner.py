"""The owner, as code proves them: an unfreeze, a
pass, an answer to an approval and an answer on Bastet's banner each take an `Owner`,
and only three places make one: `session` (guard/api.py, a logged-in session, never a
token), `bridged` (bridge/service.py, their own message in the bridged chat: from_me
and without its marker) and `precedent` (guard/learn.py: an unfreeze only, on freezes
of the same kind that they themselves ended, read back from the table). No tool, model,
intercom message or Bastet's chat holds one, so none of them can do these;
tests/test_gates_hold.py checks that no other file makes one.
"""
from __future__ import annotations

import json

from ..errors import CelineError
from . import store as decisions

_SEAL = object()


class Owner:
    __slots__ = ("via", "ids")

    def __init__(self, via: str, seal: object, ids: tuple[int, ...] = ()):
        if seal is not _SEAL:
            raise TypeError("only the owner's session or their own bridged message")
        self.via, self.ids = via, ids

    @property
    def lifts_only(self) -> bool:
        """Bastet's own lift (their precedent, a second look): an unfreeze and nothing
        else; `ids` the freezes or the row that prove it."""
        return self.via in ("precedent", "review")


def session(request) -> Owner:
    """A logged-in session's request: the owner. A token is refused (passkey_only)."""
    from ..api.deps import person_only
    person_only(request)
    return Owner("session", _SEAL)


def session_or_none(request) -> Owner | None:
    """session(), None when the request is no logged-in session (a token, a script)."""
    try:
        return session(request)
    except CelineError:
        return None


def bridged() -> Owner:
    """Their own message in the bridged chat (bridge/service.py read, from_me only)."""
    return Owner("bridge", _SEAL)


def must(by, *, unfreeze: bool = False) -> None:
    """`by` is the owner; their precedent and a second look (Bastet's lifts) count only for
    an unfreeze."""
    if not isinstance(by, Owner) or (by.lifts_only and not unfreeze):
        raise TypeError("only the owner's session or their own bridged message")


def second_look(c, rid: int) -> Owner:
    """The second look said no to the finding that froze it (guard/chain.py): read back
    here from row `rid` (its last chain level L2 with verdict N, the row froze). It
    unfreezes and does nothing else."""
    r = decisions.row(c, rid)
    last = ((json.loads(r["scores"] or "{}") or {}).get("chain") or [{}])[-1] if r else {}
    if not r or not r["froze"] or last.get("level") != "L2" or last.get("verdict") != "N":
        raise TypeError("only a second look that said no")
    return Owner("review", _SEAL, (int(rid),))


def precedent(c, ids: list[int], need: int) -> Owner:
    """Their own past unfreezes as proof (guard/learn.py): `ids` are freezes they ended
    themselves (unfrozen_by 'user', read here from `c`), at least `need` of them. It
    unfreezes and does nothing else (freeze.unfreeze records it as Bastet's lift)."""
    ids = sorted({int(i) for i in ids})
    marks = ",".join("?" * len(ids)) or "NULL"
    mine = c.execute(f"SELECT count(*) FROM freezes WHERE id IN ({marks}) AND "
                     "unfrozen_by='user'", ids).fetchone()[0]
    if need < 1 or len(ids) < need or mine != len(ids):
        raise TypeError("only freezes the owner ended themselves")
    return Owner("precedent", _SEAL, tuple(ids))
