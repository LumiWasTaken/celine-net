"""The triggers the skills declare with the user's state, and the background tasks
(docs/ARCHITECTURE.md, Triggers)."""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..errors import NotFound
from . import Event, declared, state

router = APIRouter(prefix="/api")


def _fired(c, name: str) -> str | None:
    r = c.execute("SELECT max(created_at) FROM intercom WHERE kind = 'trigger' AND "
                  "json_extract(data, '$.trigger') = ?", (name,)).fetchone()
    return r[0]


async def _view(request: Request, skill, t) -> dict:
    cfg = request.app.state.services.cfg
    return {"name": t.name, "label": cfg.substitute(t.label),
            "about": cfg.substitute(t.about), "skill": skill.name,
            "kind": "event" if isinstance(t, Event) else "watch", **state(cfg, t),
            "fired_at": await request.app.state.services.store.read(lambda c: _fired(c, t.name))}


@router.get("/triggers")
async def list_triggers(request: Request) -> dict:
    """{triggers: [{name, label, about, skill, kind: event|watch, on, fired_at}]}: every
    trigger a loaded skill declares (its label and one line on what it does), with the
    user's state and when it last fired (null: never)."""
    return {"triggers": [await _view(request, skill, t) for skill, t in
                         declared(request.app.state.services.registry).values()]}


@router.put("/triggers/{name}")
async def put_trigger(request: Request, name: str) -> dict:
    """{on}: the whole state of one trigger. Written to var/settings/triggers.toml; the
    reload starts the triggers again with it. -> the trigger as in the list, and
    `applied`."""
    from ..api.deps import json_body
    from .. import reload
    from ..settings import write
    from ..settings.files import Invalid
    found = declared(request.app.state.services.registry).get(name)
    if found is None:
        raise NotFound("trigger")
    skill, t = found
    body = await json_body(request, need=("on",), allow=("on",))
    cfg = request.app.state.services.cfg
    if not isinstance(body["on"], bool):
        raise Invalid(f"triggers.{name}.on", "This must be true or false.")
    async with write.lock():
        out = await reload.commit(request.app.state.services, {
            "triggers": {**cfg.settings.triggers, name: {"on": body["on"]}}})
    return {**await _view(request, skill, t), **out}


@router.get("/background-conversations")
async def background(request: Request) -> dict:
    """{background: [{id, title, kind, trigger, working, last_line, updated_at}]}: the
    background tasks (not the runs, which Routines lists), newest activity first;
    `trigger` the trigger's name, `working` while a turn runs there, `last_line` its
    newest line (its log line when it ended quietly)."""
    from ..conversations.store import background as rows
    return {"background": await rows(request.app.state.services.store)}
