"""Forms: one set of routes for every form drawn from the field schema
(settings/form.py), so a client never knows whose form it is. A plugin's form is its
`settings.toml`; its values live in var/settings/<id>.toml, its secrets in
var/secrets/<id>.<key>.key.

- GET /api/forms/{id} -> {fields, actions, note?, values, secrets, version, saved,
  running}. `running`: its actions can run now (a plugin's code runs only while it is on;
  `features.changed` says when that changes). While it runs, its `copies =
  "module:function"` (services, plugin, request) -> {key: text} fills its `copy` fields.
- PUT /api/forms/{id} {values, secrets?} with If-Match -> the same view. `values` is the
  whole object (a key left out goes back to its default). A secret is set by a string,
  deleted by null, kept when absent, and never echoed: `secrets` says {state, set_at}.
- POST /api/forms/{id}/{action} {input?} -> {ok, sentence, lines?}; `lines` [{text,
  tone}] are one Dot each. An action never saves.

A save (`save`, also a plugin's own route) of a plugin with [servers] starts its runner
again, so its servers run with what was saved. Errors are 400 with `field =
"<id>.<key>"`; a stale If-Match is 412.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request, Response

from .. import plugins, reload
from ..errors import Busy, NotFound
from ..plugins import Plugin
from ..settings import files, form, views, write
from ..settings.files import Invalid, version
from .deps import etag, json_body, services_of, settings_of

router = APIRouter(prefix="/api")


def _plugin(id: str) -> Plugin:
    p = plugins.get(id)
    if p is None or p.form is None:
        raise NotFound(f"There is no form '{id}'.")
    return p


def _current(s, p: Plugin) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the saved values, the version's input: the values and each secret's state)."""
    saved = s.plugins.get(p.id, {})
    states = form.secret_states(p.id, p.form["fields"], s.secrets_root)
    return saved, {"values": saved, "secrets": states}


async def _view(request: Request, response: Response, p: Plugin) -> dict[str, Any]:
    s = settings_of(request)
    saved, current = _current(s, p)
    copies = {}
    if p.data is not None and (ref := p.form.get("copies")):
        copies = await p.attr(ref)(services_of(request), p, request)
    out = {**form.view(p.id, p.form["fields"], saved, s.secrets_root, copies),
           "actions": form.actions_view(p.form.get("actions", [])),
           "version": etag(response, version(current)), "saved": bool(s.saved.get(p.id)),
           "running": p.data is not None}
    if p.form.get("note"):
        out["note"] = p.form["note"]
    return out


async def save(services, p: Plugin, values: Any, secrets: Any,
               if_match: str | None = None) -> dict[str, Any]:
    """Validate and save plugin `p`'s whole `values` and the `secrets` given; `if_match`:
    the version the client saw (None: no check). -> reload.commit's answer."""
    s = services.cfg.settings
    async with write.lock():
        _, current = _current(s, p)
        files.check_version(p.manifest["label"], current, if_match)
        clean = form.validate(p.id, p.form["fields"], values)
        checked = form.check_secrets(p.id, p.form["fields"], secrets)
        if checked and s.secrets_root is None:
            raise Invalid(f"{p.id}.secrets", "This instance keeps no secrets.")
        with form.written(p.id, s.secrets_root, checked):   # a failed save puts them back
            out = await reload.commit(services, {p.id: clean})
    if p.manifest.get("servers"):
        await views.apply_runners(services, {p.id})
    return out


@router.get("/forms/{id}")
async def get_form(request: Request, response: Response, id: str) -> dict:
    return await _view(request, response, _plugin(id))


@router.put("/forms/{id}")
async def put_form(request: Request, response: Response, id: str,
                   if_match: str | None = Header(default=None)) -> dict:
    p = _plugin(id)
    body = await json_body(request, need=("values",), allow=("values", "secrets"))
    out = await save(services_of(request), p, body["values"], body.get("secrets", {}),
                     if_match)
    return {**await _view(request, response, p), **out}


@router.post("/forms/{id}/{action}")
async def run_action(request: Request, id: str, action: str) -> dict:
    p = _plugin(id)
    a = next((a for a in p.form.get("actions", []) if a["id"] == action), None)
    if a is None:
        raise NotFound(f"There is no action '{action}' on {p.manifest['label']}.")
    body = await json_body(request, allow=("input",)) if await request.body() else {}
    if p.data is None:
        raise Busy(f"{p.manifest['label']} is not running.")
    return await p.attr(a["run"])(services_of(request), p, body.get("input"))
