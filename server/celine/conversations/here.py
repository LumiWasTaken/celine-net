"""Where it works. `where`: the conversation this turn runs in, from ids alone (its kind
and intercom address, the place, the device its tools run on, the model role, who started
it, who it reports to and how deep it is in the chain of agents), in its system prompt:
none of it changes while the conversation lives, so the prompt's start stays the same.
`busy`: the other conversations of its own working right now, in the stored tail of a
turn when there are any (turns/prompt.py). So it knows where it is and where else it is
busy, and can write there."""
from __future__ import annotations

import sqlite3

from ..core.states import LIVE_SQL
from ..intercom.store import OPENING

OTHERS_MAX = 6
COLS = "id, kind, title, trigger_key, agent_name, agent_of, agent_cli, agent_device"


def _run(c: sqlite3.Connection, r) -> sqlite3.Row | None:
    """The run of a Routine's conversation, None for any other."""
    if not (r["trigger_key"] or "").startswith("run:"):
        return None
    return c.execute("SELECT id, schedule, label FROM runs WHERE conversation_id=? "
                     "ORDER BY slot DESC LIMIT 1", (r["id"],)).fetchone()


def _what(c: sqlite3.Connection, r) -> str:
    """One conversation in its words, with its address; titles and names as they are now
    (for the tail)."""
    title = f"\"{r['title']}\"" if r["title"] else "untitled"
    if (run := _run(c, r)) is not None:
        return f"the run of the Routine {run['label']} (address run:{run['id']})"
    if r["kind"] == "mission":
        m = c.execute("SELECT name FROM missions WHERE conversation_id=?",
                      (r["id"],)).fetchone()
        return f"the mission {m['name'] if m else title} (address {r['id']})"
    if r["kind"] == "agent":
        return f"the sub-agent {r['agent_name'] or title} (address {r['id']})"
    if r["kind"] == "chat":
        return f"the chat {title} (address {r['id']})"
    return f"the background task {title} (address {r['id']})"


def _ids(c: sqlite3.Connection, r) -> str:
    """One conversation by what never changes: its kind, a sub-agent's name (unique
    under its parent, set once), a run's Routine and its address."""
    if (run := _run(c, r)) is not None:
        return f"a run of the Routine {run['schedule']} (address run:{run['id']})"
    kind = {"chat": "a chat", "mission": "a mission", "background": "a background task",
            "agent": f"the sub-agent {r['agent_name']}"}.get(r["kind"], r["kind"])
    return f"{kind} (address {r['id']})"


def chain(c: sqlite3.Connection, cid: str) -> list[sqlite3.Row]:
    """The conversation and the ones above it on the agent_of chain, itself first."""
    out: list[sqlite3.Row] = []
    seen: set[str] = set()
    while cid and cid not in seen:
        seen.add(cid)
        r = c.execute(f"SELECT {COLS} FROM conversations WHERE id=?", (cid,)).fetchone()
        if r is None:
            break
        out.append(r)
        cid = r["agent_of"]
    return out


def where(c: sqlite3.Connection, cid: str, role: str | None = None) -> str | None:
    """Where it works, one fact a line, from ids; None for a conversation that is gone.
    `role`: the model role its turns run on."""
    from ..skills.places import place_of
    up = chain(c, cid)
    if not up:
        return None
    from .store import depth as depth_of
    me, depth = up[0], depth_of(c, cid)
    place = place_of(c, cid) + (", as a sub-agent" if me["kind"] == "agent" else "")
    from ..devices import label
    device = (f"{label(me['agent_device'])}, through {me['agent_cli']}"
              if me["agent_device"] else
              f"your box, through {me['agent_cli']}" if me["agent_cli"]
              else "the house (the app's own server)")
    starter = boss = "{{username}}"
    if me["kind"] == "agent" and len(up) > 1:
        starter = boss = _ids(c, up[1])
    elif me["kind"] == "mission":
        r = c.execute("SELECT started_by FROM missions WHERE conversation_id=?",
                      (cid,)).fetchone()
        parent = r and r[0] and c.execute(f"SELECT {COLS} FROM conversations WHERE id=?",
                                          (r[0],)).fetchone()
        if parent:
            starter = boss = _ids(c, parent)
    elif (run := _run(c, me)) is not None:
        starter = f"the Routine {run['schedule']}"
    elif (me["trigger_key"] or "").startswith("trigger:"):
        starter = f"the trigger {me['trigger_key'].removeprefix('trigger:')}"
    elif me["kind"] == "chat" and c.execute("SELECT 1 FROM kv WHERE k=?",
                                            (OPENING + cid,)).fetchone():
        starter = "you, with intercom__start_chat"
    lines = [f"Where you are: {_ids(c, me)}.", f"Place: {place}.", f"Device: {device}."]
    if role:
        lines.append(f"Model role: {role}.")
    lines += [f"Started by: {starter}.", f"You report to: {boss}.",
              f"Depth: {depth} (0 is a conversation of your own, each sub-agent one more "
              f"than the one that started it)."]
    return "\n".join(lines)


def busy(c: sqlite3.Connection, cid: str) -> str | None:
    """The other conversations working now, None when none is."""
    others = c.execute(
        f"SELECT DISTINCT c.id, c.kind, c.title, c.trigger_key, c.agent_name, c.agent_of "
        f"FROM turns t JOIN conversations c ON c.id = t.conversation_id "
        f"WHERE t.state IN {LIVE_SQL} AND c.id != ? AND c.trashed_at IS NULL "
        f"ORDER BY t.created_at LIMIT ?", (cid, OTHERS_MAX)).fetchall()
    return "[also working now] " + "; ".join(_what(c, o) for o in others) + "." \
        if others else None
