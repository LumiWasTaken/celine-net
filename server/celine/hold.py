"""Why none of its turns may start now: the one question the dispatcher asks before it
claims a turn (turns/dispatcher.py). "frozen": the user or Bastet froze the agent
(guard/freeze.py); the unfreeze wakes the workers. "paused": the gateway sees no host of
the chat and tasks models answer (backends/router.py); it resumes by itself when one
is back. Frozen wins over paused. Routines and messages still queue their turns; they
wait here. Paused holds the agent's own turns only: a sub-agent's or a drone's turn whose own
model answers is still claimed (turns/store.py claim, Router.answering).

`held_sql` is that rule for one queued turn, as SQL over its conversation `c`: what the
lists (conversations/store.py), the snapshot's `held`, the claim and `any_held` (whether
the dispatcher asks its servers again while it is paused) read."""
from __future__ import annotations

FROZEN_SQL = "EXISTS (SELECT 1 FROM kv WHERE k = 'frozen')"


async def agent_hold(store, llm) -> str | None:
    """"frozen", "paused", or None when its turns may run. `llm`: the LLM service, or
    None (no gateway, never paused)."""
    from .guard.freeze import frozen
    if await frozen(store) is not None:
        return "frozen"
    if llm is not None and llm.router.paused:
        return "paused"
    return None


def answering(llm) -> frozenset[str] | None:
    """None while the agent is not paused, else the models (refs) that still answer: a
    sub-agent's turn on one of them runs. `llm`: the LLM service, or None."""
    router = getattr(llm, "router", None)
    if router is None or not router.paused:
        return None
    return frozenset(router.answering())


def held_sql(models: frozenset[str] | None, c: str = "c") -> str:
    """SQL, true when a queued turn of the conversation aliased `c` waits for the hold:
    frozen, or paused (`models`: answering) and not a sub-agent whose own model is one
    of `models`."""
    if models is None:
        return FROZEN_SQL
    refs = ",".join("'" + m.replace("'", "''") + "'" for m in sorted(models))
    own = f"({c}.agent_of IS NOT NULL AND {c}.model_id IN ({refs}))" if refs else "0"
    return f"({FROZEN_SQL} OR NOT {own})"


def any_held(c, models: frozenset[str] | None) -> bool:
    """A queued turn waits for the hold now (`models` as in held_sql)."""
    return c.execute(f"SELECT 1 FROM turns t JOIN conversations c ON c.id = t.conversation_id "
                     f"WHERE t.state = 'queued' AND {held_sql(models)} LIMIT 1"
                     ).fetchone() is not None


def held(c, cid: str, models: frozenset[str] | None) -> str | None:
    """"frozen" or "paused" when a queued turn of conversation `cid` would wait for the
    hold now, else None: the snapshot's `held`."""
    if c.execute(f"SELECT {FROZEN_SQL}").fetchone()[0]:
        return "frozen"
    r = c.execute(f"SELECT {held_sql(models)} FROM conversations c WHERE c.id=?",
                  (cid,)).fetchone() if models is not None else None
    return "paused" if r and r[0] else None
