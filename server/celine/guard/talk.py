"""Bastet answers the agent on the Intercom. The agent writes to `bastet`
(intercom/addresses.py: `system:guard`); its message is a work item (work.py,
`bastet.answer`, the guard role) and Bastet answers in the same thread, in character.

What Bastet's chat model reads: her persona and talk (persona.py, the owner's to edit),
their recent messages (HISTORY, both ways), and its recent decisions about the agent as
plain lines (bastet.plain: the rule's plain words, the tool, the time, and the judge's one
sentence why). Never the rule file, a threshold, a score, a probability or a piece: it
cannot leak what it never reads, and the why lets it tell the agent what looked wrong
instead of guessing.

It never grants a pass, an approval or an unfreeze: nothing here calls one (its frame,
persona.py `talk`, tells it so). Bastet's answer lands without waking the agent
(wake false): it reads it at its next step or turn, and two talkers never wake each
other in a loop. While the agent is frozen the item waits (Frozen defers it); while no host
of the guard role answers the pool defers it (work.Pool); a message with a time is
answered when it lands.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from ..backends.queue import Priority
from ..clock import zone
from ..settings.thinking import OFF
from . import bastet, caps, persona, rules as rule_set, store as decisions, view
from .bastet import tool_words
from .freeze import frozen
from .switch import enabled

log = logging.getLogger("celine.guard")

KIND = "bastet.answer"
NOTE = "bastet.note"
NOTE_SECONDS = 45.0   # the guard model's time for one note
HISTORY = 12          # their messages it reads back, newest kept
DECIDED = 8           # its newest decisions about the agent, as plain lines
SECONDS = 60.0        # the guard model's time for one answer

DID = {"stop": "stopped", "freeze": "stopped and froze {{name}} for",
       "approved": "{{username}} allowed",
       "denied": "{{username}} said no to", "closed": "{{username}} did not answer about",
       "patrol": "looking back, found"}


class Frozen(Exception):
    """It is frozen: the item waits (work.deferrable reads `unreachable`)."""
    unreachable = True


async def heard(app, iid: int, conversation_id: str, after: float | None = None) -> None:
    """The agent's message #iid to Bastet: an answer to make, once."""
    pool = getattr(getattr(app, "dispatcher", None), "work", None)
    if pool is None or not enabled(app.cfg):
        return
    await pool.put(KIND, {"intercom_id": iid}, conversation_id=conversation_id,
                   unique=True, after=after if after and after > 0 else None)


def start(app) -> None:
    """At startup: the work items' handlers."""
    app.dispatcher.work.handle(KIND, ("guard",), lambda p: answer(app, p))
    app.dispatcher.work.handle(NOTE, ("guard",), lambda p: note(app, p))


def _guard(app, conversation_id: str | None, backend=None):
    """Bastet's chat model: the guard role in the background, on its own lane; in a test, the
    dispatcher's backend_factory or `backend`."""
    d = getattr(app, "dispatcher", None)
    if backend is None and d is not None and getattr(d, "backend_factory", None):
        backend = d.backend_factory()
    return app.llm.bind(backend, role="guard", priority=Priority.BACKGROUND,
                        conversation_id=conversation_id, lane="bastet", thinking=OFF,
                        voice=False)


# ---- its notes to the owner, in its own voice ------------------------------------------


async def voiced(app, facts: str, conversation_id: str | None, backend=None) -> str:
    """A note to the owner in Bastet's persona, written by the guard role from `facts`
    (the template line: what happened, the quote, the why, what it did), the chat named
    by its title. The template itself when no model answers in NOTE_SECONDS: this runs
    in the background, never in its turn."""
    from ..turns.compact import complete
    cfg = app.cfg
    llm = getattr(app, "llm", None)
    if llm is None:
        return facts
    title = None
    if conversation_id:
        title = await app.store.read(lambda c: (c.execute(
            "SELECT title FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            or [None])[0])
    try:
        out = await complete(_guard(app, conversation_id, backend), [
            {"role": "system", "content": await persona.frame(app.store, cfg, "note")},
            {"role": "user", "content": f"The chat: {title or 'a chat without a title'}\n"
                                        f"What happened:\n{facts}"}], NOTE_SECONDS)
    except Exception:
        log.info("bastet note in her template", exc_info=True,
                 extra={"event": "guard.note_template"})
        out = None
    return out if out and out.strip() else facts


async def note(app, p: dict) -> None:
    """The `bastet.note` item: the note in its voice, then sent."""
    text = await voiced(app, p["text"], p.get("conversation_id"))
    await bastet.tell_owner(app, text, p.get("conversation_id"), p.get("turn_id"))


async def to_owner(app, text: str, conversation_id: str | None,
                   turn_id: str | None) -> None:
    """A note from its turn's path: queued, written in its voice in the background; sent
    as the template at once when there is no work pool (a script, a test)."""
    pool = getattr(getattr(app, "dispatcher", None), "work", None)
    if pool is None:
        await bastet.tell_owner(app, text, conversation_id, turn_id)
        return
    await pool.put(NOTE, {"text": text, "conversation_id": conversation_id,
                          "turn_id": turn_id}, conversation_id=conversation_id)


async def to_agent(app, conversation_id: str, text: str) -> None:
    """Bastet's line to the agent in its chat that wakes it (D15: the owner allowed a
    stopped step once or passed it, so it does the step again now)."""
    text = app.cfg.substitute(text)
    try:
        if getattr(app, "intercom", None) is not None:
            await app.intercom.send(bastet.ADDR, f"agent:{conversation_id}", text, wake=True)
        else:
            from ..intercom import send
            await send(app.store, bastet.ADDR, f"agent:{conversation_id}", text, wake=True)
    except KeyError:                         # its conversation is gone
        log.info("bastet line dropped", extra={"event": "guard.to_agent_dropped"})


# ---- what it reads -------------------------------------------------------------------

def decided(c: sqlite3.Connection, cfg, tz) -> list[str]:
    """Bastet's newest decisions about the agent that it heard of (mode On), oldest first,
    each a plain line: when, what it did, on which tool, the rule's plain words, the
    judge's why."""
    out = []
    for r in decisions.decided(c, DECIDED):
        kind = "stop" if r["rule"] == "gates" else view.kind(r)   # a closed gate stopped it
        if kind not in DID:
            continue
        when = f"{datetime.fromisoformat(r['created_at']).astimezone(tz):%a %H:%M}"
        what = bastet.sub(cfg, rule_set.plain(r["rule"]))
        on = tool_words(r["tool"])
        why = " ".join((r["why"] or "").split()) if r["decision"] not in (
            "failed", "unreachable") else ""
        out.append(f"{when}: {bastet.sub(cfg, DID[kind])} {on}: {what}"
                   + (f". Why: {why}" if why else ""))
    return list(reversed(out))


def prompt(cfg, system: str, said: list[tuple[str, str]], lines: list[str],
           text: str) -> list[dict]:
    """The guard model's messages: who Bastet is and how she answers (`system`,
    persona.frame), what it decided lately, their messages, the agent's new one."""
    name = cfg.substitute("{{name}}")
    talk = "\n".join(f"{name if who == 'agent' else 'You'}: "
                     f"{caps.head(' '.join(t.split()), caps.MESSAGE)}"
                     for who, t in said) or "(nothing yet)"
    done = "\n".join(f"- {d}" for d in lines) or "- nothing lately"
    return [{"role": "system", "content": system},
            {"role": "user", "content": f"What you did about {name} lately:\n{done}\n\n"
                                        f"Your messages with {name}, oldest first:\n{talk}\n\n"
                                        f"{name} writes now:\n"
                                        f"{caps.head(text, caps.MESSAGE)}"}]


# ---- the work item --------------------------------------------------------------------

async def answer(app, p: dict, backend=None) -> str | None:
    """The `bastet.answer` item: its reply in the thread of #intercom_id. -> what it
    said, None when there is nothing to answer (gone, cancelled, answered). Frozen:
    raised, the item waits. A model error is raised (no host up defers the item).
    `backend`: a fixed one (tests)."""
    from ..turns.compact import complete
    store, cfg, iid = app.store, app.cfg, int(p["intercom_id"])
    if await frozen(store):
        raise Frozen("the agent is frozen")
    tz = zone(cfg)

    def read(c: sqlite3.Connection):
        if (m := decisions.to_bastet(c, iid)) is None:
            return None
        return m["from_addr"], m["text"], decisions.talk(c, iid, HISTORY), decided(c, cfg, tz)
    got = await store.read(read)
    if got is None:
        return None
    sender, text, said, lines = got
    cid = sender[6:]
    if app.llm is None:                      # no model layer (a script)
        return None
    system = await persona.frame(store, cfg, "talk")
    out = await complete(_guard(app, cid, backend), prompt(cfg, system, said, lines, text),
                         SECONDS, errors=True)
    if not out or not out.strip():
        raise TimeoutError("Bastet's model gave no answer")      # a try; again later
    reply = out

    def first(c: sqlite3.Connection) -> bool:        # once: in the send's own write
        return not decisions.answered(c, iid)
    # its first message to the agent ever says who it is
    if not await decisions.ever_wrote_agent(store):
        reply = bastet.fill(cfg, bastet.INTRO) + " " + reply
    try:
        if getattr(app, "intercom", None) is not None:
            sent = await app.intercom.send(bastet.ADDR, sender, reply, reply_to=iid,
                                           wake=False, only_if=first)
        else:
            from ..intercom import send
            sent = await send(store, bastet.ADDR, sender, reply, reply_to=iid,
                              wake=False, only_if=first)
    except KeyError:                         # its conversation is gone
        log.info("bastet answer dropped", extra={"event": "guard.answer_dropped"})
        sent = None
    await decisions.read_by_bastet(store, iid)
    return reply if sent is not None else None
