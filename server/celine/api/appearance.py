"""Settings, Appearance: GET and PUT /api/appearance (; the
choices in settings/appearance.py APPEARANCE, stored in var/settings/appearance.toml). Every
key comes back with its effective value; a PUT takes the keys to change (null: back to its
default, so the defaults stay the server's), and a save sends
`settings.changed` (what: ["appearance"]) so every open page shows it at once."""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..settings import appearance as appearance_mod, files
from ..settings import write
from .deps import json_body

router = APIRouter(prefix="/api")


def _root(request: Request):
    return files.settings_dir(request.app.state.services.cfg)


@router.get("/appearance")
async def get_appearance(request: Request) -> dict:
    return {"appearance": appearance_mod.effective(appearance_mod.read(_root(request)))}


@router.put("/appearance")
async def put_appearance(request: Request) -> dict:
    body = await json_body(request, need=("appearance",), allow=("appearance",))
    change = appearance_mod.validate(body["appearance"])
    async with write.lock():
        root = _root(request)
        saved = {k: v for k, v in {**appearance_mod.read(root), **change}.items() if v is not None}
        files.write(root, "appearance", saved)
    write.publish(request.app.state.services, ["appearance"])
    return {"appearance": appearance_mod.effective(saved)}
