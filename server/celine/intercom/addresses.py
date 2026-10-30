"""Who a message goes to. Stored addresses: `user`, `agent:<conversation id>`,
`team:<conversation id>` (that conversation and its sub-agents), `all` (everyone
working), `system:guard` (Bastet, the guard: she answers her, guard/talk.py),
`system:<name>` (a sender only: a host script of the user's, so writing to it, by that
address or its token's name, reaches the user).

`to` takes several, comma separated. The agent's words: `me` (the sender's own conversation),
`parent` (the conversation that started a sub-agent), `team` (the sender's own
sub-agents), `all`, `user`, `bastet`,
a conversation id, the name of a sub-agent or mission, `run:<id>` (a Routine's run) or
`routine:<name>` (its newest run with a conversation). `team` and `all` are expanded
into recipients when the message is sent, the sender left out.

A sub-agent writes up and down its own chain only: to itself, its parent, its own
sub-agents, the user and Bastet (`_reach`)."""
from __future__ import annotations

import sqlite3

from ..core.states import LIVE_SQL


def _conversation(c: sqlite3.Connection, cid: str) -> sqlite3.Row | None:
    return c.execute("SELECT id, agent_of FROM conversations WHERE id=?", (cid,)).fetchone()


def members(c: sqlite3.Connection, root: str) -> list[str]:
    """Conversation `root` and its sub-agents, oldest first."""
    return [root] + [r[0] for r in c.execute(
        "SELECT id FROM conversations WHERE agent_of=? ORDER BY created_at", (root,))]


GUARD = "system:guard"                  # Bastet (guard/bastet.py ADDR)
GUARD_NAMES = ("bastet", GUARD)


class Refused(ValueError):
    """An address nobody but the user may write to; its sentence is the whole answer."""


def _locked(c: sqlite3.Connection, cid: str, sender: str) -> None:
    """Refused: a locked conversation (conversations/store.locked), written to from
    another conversation of its own. The user writes anywhere; what the app or a locked
    conversation sends there is dropped when it would land (deliver.py)."""
    from ..conversations.store import locked
    if not sender.startswith("agent:") or sender == f"agent:{cid}" or \
            locked(c, sender[6:]):
        return
    if why := locked(c, cid):
        raise Refused(why)


REACH = ('A sub-agent writes to "parent", its own sub-agents (by name, or "team" for '
         'all of them), "me", "user" and "bastet".')


def _reach(c: sqlite3.Connection, cid: str, me: sqlite3.Row | None) -> None:
    """Refused: sub-agent `me` writing to a conversation other than itself, its parent
    and its own sub-agents."""
    if me is None or not me["agent_of"] or cid in (me["id"], me["agent_of"]):
        return
    if c.execute("SELECT 1 FROM conversations WHERE id=? AND agent_of=?",
                 (cid, me["id"])).fetchone() is None:
        raise Refused(REACH)


def _working(c: sqlite3.Connection) -> list[str]:
    """Every conversation with a live turn or a running job, and every active mission;
    nothing locked."""
    from ..conversations.store import locked_sql
    return [r[0] for r in c.execute(
        f"SELECT conversation_id FROM turns WHERE state IN {LIVE_SQL} "
        "UNION SELECT conversation_id FROM jobs WHERE state='running' "
        "UNION SELECT conversation_id FROM missions WHERE status='active' "
        f"EXCEPT SELECT id FROM conversations c WHERE {locked_sql()}")]


def _by_name(c: sqlite3.Connection, name: str, me: sqlite3.Row | None) -> str | None:
    """A sub-agent of the sender's own, or a mission, by name."""
    if me is not None and (r := c.execute(
            "SELECT id FROM conversations WHERE agent_of=? AND agent_name=?",
            (me["id"], name)).fetchone()):
        return r[0]
    r = c.execute("SELECT conversation_id FROM missions WHERE name=?",
                  (name,)).fetchone()
    return r[0] if r else None


def _run(c: sqlite3.Connection, addr: str) -> str | None:
    """The conversation of `run:<id>` or of `routine:<name>`'s newest run that has one.
    ValueError: no such run, or it has not started."""
    kind, _, key = addr.partition(":")
    key = key.strip()
    r = c.execute("SELECT conversation_id FROM runs WHERE id=?", (key,)).fetchone() \
        if kind == "run" else c.execute(
            "SELECT conversation_id FROM runs WHERE schedule=? AND conversation_id IS NOT "
            "NULL ORDER BY slot DESC LIMIT 1", (key,)).fetchone()
    if r is None:
        raise ValueError(f"there is no {'run' if kind == 'run' else 'routine run'} {key!r}")
    if r[0] is None:
        raise ValueError(f"run {key} has not started yet")
    return r[0]


def resolve(c: sqlite3.Connection, to: str, sender: str,
            store=None) -> tuple[str, list[tuple[str, str | None]]]:
    """-> (`to` as stored, [(recipient address, conversation id or None)]), in the
    order written, once each. ValueError: an address that does not read (Refused: a
    locked conversation, written to by another of its own). KeyError: no such
    conversation."""
    me = _conversation(c, sender[6:]) if sender.startswith("agent:") else None
    stored: list[str] = []
    out: list[tuple[str, str | None]] = []

    def add(cid: str) -> None:
        _reach(c, cid, me)
        _locked(c, cid, sender)
        if f"agent:{cid}" != sender and (f"agent:{cid}", cid) not in out:
            out.append((f"agent:{cid}", cid))

    for word in to.split(","):
        addr = " ".join(word.split())
        if not addr:
            continue
        if addr in ("me", "parent", "team") and me is None:
            raise ValueError(f"`{addr}` needs a conversation that sends")
        if addr == "me":
            addr = sender
        elif addr == "parent":
            if not me["agent_of"]:
                raise ValueError("`parent` is for a sub-agent: this conversation has none")
            addr = f"agent:{me['agent_of']}"
        elif addr == "team":
            addr = f"team:{me['id']}"
        elif addr in GUARD_NAMES and sender.startswith("agent:"):
            addr = GUARD                 # she talks to Bastet; anyone else's reply is the user's
        elif addr.startswith("system:") or c.execute(
                "SELECT 1 FROM intercom_tokens WHERE name=?", (addr,)).fetchone():
            addr = "user"
        if addr in ("user", GUARD):
            if (addr, None) not in out:
                out.append((addr, None))
        elif addr == "all":
            if me is not None and me["agent_of"]:
                raise Refused(REACH)
            for cid in _working(c):
                add(cid)
        elif addr.startswith("team:"):
            if _conversation(c, addr[5:]) is None:
                raise KeyError(addr[5:])
            for cid in members(c, addr[5:]):
                add(cid)
        else:
            cid = _run(c, addr) if addr.startswith(("run:", "routine:")) else \
                addr[6:] if addr.startswith("agent:") else addr
            if _conversation(c, cid) is None:
                cid = None if addr.startswith("agent:") else _by_name(c, addr, me)
                if cid is None:
                    if addr.startswith("agent:"):
                        raise KeyError(addr[6:])
                    raise ValueError(f"{addr!r} is not an address")
            _reach(c, cid, me)
            _locked(c, cid, sender)
            addr = f"agent:{cid}"
            # to yourself is a reminder: the sender is not left out here
            if (addr, cid) not in out:
                out.append((addr, cid))
        if addr not in stored:
            stored.append(addr)
    if not stored:
        raise ValueError("no address")
    return ", ".join(stored), out
