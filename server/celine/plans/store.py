"""Plan rows (table `plans`): {text, steps, waiting, updated_at}, each step
{title, state, checkpoint, model, started_at, done_at}. The rules live here, nowhere
else: at most one step `doing`; while a step is open one is doing (the first open one
when none is said); `waiting` is set when an `ask` step is done, and cleared by the next
change. A step's times: started_at when it becomes doing, done_at when it becomes done,
both gone when it is todo again; a step plan__set writes again under the same title
keeps them.

Every change is one write that appends `plan.state` {plan} to the conversation (the
transcript pins it) and, for a mission, its `mission.state` on the global feed."""
from __future__ import annotations

import json
import sqlite3

from ..core.ids import now
from ..core.event_types import PLAN_STATE

STATES = ("todo", "doing", "done")
CHECKPOINTS = ("none", "ask")
TITLE_MAX = 200
STEPS_MAX = 30


class Refused(ValueError):
    """A plan that breaks a rule; its sentence is what it reads."""


def view(r) -> dict | None:
    if r is None:
        return None
    return {"conversation_id": r["conversation_id"], "text": r["text"],
            "steps": json.loads(r["steps"]), "waiting": bool(r["waiting"]),
            "updated_at": r["updated_at"]}


def get_sync(c: sqlite3.Connection, cid: str) -> dict | None:
    return view(c.execute("SELECT * FROM plans WHERE conversation_id=?", (cid,)).fetchone())


async def get(store, cid: str) -> dict | None:
    return await store.read(lambda c: get_sync(c, cid))


def is_open(plan: dict | None) -> bool:
    """A step is not done yet."""
    return bool(plan) and any(s["state"] != "done" for s in plan["steps"])


def doing(plan: dict | None) -> int | None:
    """The number (1 for the first) of the step it is on, None when none is."""
    for n, s in enumerate((plan or {}).get("steps") or (), 1):
        if s["state"] == "doing":
            return n
    return None


def _mark(s: dict, state: str) -> None:
    """Step `s` becomes `state`, its times kept with it."""
    s["state"] = state
    if state == "todo":
        s["started_at"] = s["done_at"] = None
    elif state == "doing":
        s["started_at"], s["done_at"] = s.get("started_at") or now(), None
    elif not s.get("done_at"):
        s["done_at"] = now()


def clean(steps, before: list[dict] = ()) -> list[dict]:
    """Steps as stored, the rules applied; a step whose title is in `before` (the plan
    this one replaces) keeps its times. Refused with the sentence to show."""
    if not isinstance(steps, list):
        raise Refused("steps is a list of steps.")
    if len(steps) > STEPS_MAX:
        raise Refused(f"A plan has at most {STEPS_MAX} steps.")
    kept = {s["title"]: s for s in before}
    out = []
    for n, s in enumerate(steps, 1):
        if isinstance(s, str):
            s = {"title": s}
        if not isinstance(s, dict):
            raise Refused(f"Step {n} is {{\"title\": ...}}.")
        title = " ".join(str(s.get("title") or "").split())
        if not title:
            raise Refused(f"Step {n} has no title.")
        if len(title) > TITLE_MAX:
            raise Refused(f"Step {n}: a title is at most {TITLE_MAX} characters.")
        state = s.get("state") or "todo"
        checkpoint = s.get("checkpoint") or "none"
        if state not in STATES:
            raise Refused(f"Step {n}: state is todo, doing or done.")
        if checkpoint not in CHECKPOINTS:
            raise Refused(f"Step {n}: checkpoint is none or ask.")
        was = kept.get(title, {})
        step = {"title": title, "state": state, "checkpoint": checkpoint,
                "model": str(s.get("model") or "").strip() or None,
                "started_at": was.get("started_at"), "done_at": was.get("done_at")}
        _mark(step, state)
        out.append(step)
    on = [n for n, s in enumerate(out, 1) if s["state"] == "doing"]
    if len(on) > 1:
        raise Refused(f"Only one step is doing at a time; steps {on[0]} and {on[1]} are.")
    if not on:
        nxt = next((s for s in out if s["state"] == "todo"), None)
        if nxt is not None:
            _mark(nxt, "doing")
    return out


def save_sync(c: sqlite3.Connection, store, cid: str, text: str, steps: list[dict],
              waiting: bool = False) -> dict:
    """Inside a write: the plan as given (steps already clean), told. -> its view."""
    c.execute("INSERT INTO plans(conversation_id, text, steps, waiting, updated_at) "
              "VALUES(?,?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET "
              "text=excluded.text, steps=excluded.steps, waiting=excluded.waiting, "
              "updated_at=excluded.updated_at",
              (cid, text, json.dumps(steps, ensure_ascii=False), int(waiting), now()))
    plan = get_sync(c, cid)
    store.event(c, cid, PLAN_STATE, {"plan": plan})
    from ..missions import store as missions
    row = missions.row_sync(c, cid)
    if row is not None:
        event = missions.state_event(row)
        store.global_event(c, event.pop("type"), event)
    return plan


def answered_sync(c: sqlite3.Connection, store, cid: str) -> None:
    """Inside a write: the user spoke in `cid` (a message, a reply on the intercom): a
    plan waiting at a checkpoint goes on."""
    plan = get_sync(c, cid)
    if plan is not None and plan["waiting"]:
        save_sync(c, store, cid, plan["text"], plan["steps"], False)


async def save(store, cid: str, text: str, steps: list[dict],
               waiting: bool = False) -> dict:
    return await store.write(lambda c: save_sync(c, store, cid, text, steps, waiting))


def finish_sync(c: sqlite3.Connection, store, cid: str) -> None:
    """Inside a write: its turn ended with its answer (turns/store.transition). When the
    step it is on is the last open one, its answer finished it: done, the same write and
    event as plan__step."""
    plan = get_sync(c, cid)
    n = doing(plan)
    if n is None or any(s["state"] != "done" for i, s in enumerate(plan["steps"], 1)
                        if i != n):
        return
    steps, waiting = move(plan, n, "done")
    save_sync(c, store, cid, plan["text"], steps, waiting)


def move(plan: dict, n: int, state: str) -> tuple[list[dict], bool]:
    """Step `n` set to `state`. -> (the steps, waiting). Done on an `ask` step waits on
    the user and starts nothing; done on another starts the next open one. Refused:
    no such step, or another step is doing."""
    steps = [dict(s) for s in plan["steps"]]
    if not steps:
        raise Refused("The plan has no steps: plan__set first.")
    if not 1 <= n <= len(steps):
        raise Refused(f"step is a number from 1 to {len(steps)}.")
    if state not in STATES:
        raise Refused("state is todo, doing or done.")
    on = doing(plan)
    if state == "doing" and on not in (None, n):
        raise Refused(f"Step {on} is doing. Finish it first: plan__step({on}, \"done\").")
    _mark(steps[n - 1], state)
    waiting = state == "done" and steps[n - 1]["checkpoint"] == "ask"
    if state == "done" and not waiting and on in (None, n):
        nxt = next((s for s in steps if s["state"] == "todo"), None)
        if nxt is not None:
            _mark(nxt, "doing")
    return steps, waiting
