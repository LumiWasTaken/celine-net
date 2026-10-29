"""Forms: one set of routes for every form drawn from the field schema
(settings/form.py), so a client never knows whose form it is. A plugin's form is its
`settings.toml`; its values live in var/settings/<id>.toml, its secrets in
var/secrets/<id>.<key>.key.

- GET /api/forms/{id} -> {fields, actions, note?, values, secrets, version, saved,
  running}. `running`: its actions can run now (a plugin's code runs only while it is on;
  `features.changed` says when that changes).
- PUT /api/forms/{id} {values, secrets?} with If-Match -> the same view. `values` is the
  whole object (a key left out goes back to its default). A secret is set by a string,
  deleted by null, kept when absent, and never echoed: `secrets` says {state, set_at}.
- POST /api/forms/{id}/{action} {input?} -> {ok, sentence}. An action never saves.

Errors are 400 with `field = "<id>.<key>"`; a stale If-Match is 412.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request, Response

from .. import plugins, reload
from ..errors import Busy, NotFound
from ..plugins import Plugin
from ..settings import files, form, write
from ..settings.files import Invalid, version
from .deps import etag, json_body, services_of, settings_of

router = APIRouter(prefix="/api")


def _plugin(id: str) -> Plugin:
    p = plugins.get(id)
    if p is None or p.form is None:
        raise NotFound(f"There is no form '{id}'.")
    return p


def _current(request: Request, p: Plugin) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the saved values, the version's input: the values and each secret's state)."""
    s = settings_of(request)
    saved = s.plugins.get(p.id, {})
    states = form.secret_states(p.id, p.form["fields"], s.secrets_root)
    return saved, {"values": saved, "secrets": states}


def _view(request: Request, response: Response, p: Plugin) -> dict[str, Any]:
    saved, current = _current(request, p)
    s = settings_of(request)
    out = {**form.view(p.id, p.form["fields"], saved, s.secrets_root),
           "actions": form.actions_view(p.form.get("actions", [])),
           "version": etag(response, version(current)), "saved": bool(s.saved.get(p.id)),
           "running": p.data is not None}
    if p.form.get("note"):
        out["note"] = p.form["note"]
    return out


@router.get("/forms/{id}")
async def get_form(request: Request, response: Response, id: str) -> dict:
    return _view(request, response, _plugin(id))


@router.put("/forms/{id}")
async def put_form(request: Request, response: Response, id: str,
                   if_match: str | None = Header(default=None)) -> dict:
    p = _plugin(id)
    body = await json_body(request, need=("values",), allow=("values", "secrets"))
    async with write.lock():
        _, current = _current(request, p)
        files.check_version(p.manifest["label"], current, if_match)
        clean = form.validate(id, p.form["fields"], body["values"])
        secrets = form.check_secrets(id, p.form["fields"], body.get("secrets", {}))
        root = settings_of(request).secrets_root
        if secrets and root is None:
            raise Invalid(f"{id}.secrets", "This instance keeps no secrets.")
        with form.written(id, root, secrets):       # a failed save puts them back
            out = await reload.commit(services_of(request), {id: clean})
        return {**_view(request, response, p), **out}


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
