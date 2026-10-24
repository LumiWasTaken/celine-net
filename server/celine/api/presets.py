"""The user's presets (Settings, Images & Music; comfyui/presets.py), changed over HTTP:

- POST /api/generation-presets {kind, label, workflow (the file's text)}: the user's own
  preset from a workflow with placeholders (presets.upload), at the end of its role;
- PUT /api/generation-presets/{id} {values}: the editor's values, saved as the user's;
- PUT /api/generation-presets/{id}/workflow {workflow}: another workflow for it;
- GET /api/generation-presets/{id}/workflow: the workflow with its placeholders, a file;
- POST /api/generation-presets/{id}/reset: an edited shipped preset as shipped;
- DELETE /api/generation-presets/{id}: the user's own preset, out of its role too.

Every refusal is 400 with each reason, and nothing is saved. Each answer is the preset
as GET /api/generation-presets lists it (comfyui/api.py).
"""
import json
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from .. import reload
from ..comfyui import presets as presets_mod
from ..errors import CelineError, NotFound
from ..settings import write

router = APIRouter(prefix="/api")


class UploadIn(BaseModel):
    kind: Literal["images", "music"]
    label: str
    workflow: str


class ValuesIn(BaseModel):
    values: dict[str, Any]


class WorkflowIn(BaseModel):
    workflow: str


def _preset(pid: str) -> presets_mod.Preset:
    kind = presets_mod.kind_of(pid)
    if kind is None:
        raise NotFound("preset")
    return presets_mod.of(kind)[pid]


async def _saved(request: Request, p: presets_mod.Preset, changes: dict | None = None
                 ) -> dict:
    """After a preset's files changed: the settings `changes` (its role, its provider)
    committed, else the config loaded again; every page hears it."""
    if changes:
        await reload.commit(request.app.state.services, changes)
    else:
        reload.apply(request.app.state.services)
    write.publish(request.app.state.services, ["presets"])
    return next(x for x in presets_mod.listing(request.app.state.services.cfg.settings, p.kind) if x["id"] == p.id)


@router.post("/generation-presets", status_code=201)
async def upload_preset(request: Request, body: UploadIn) -> dict:
    async with write.lock():
        p = presets_mod.upload(body.kind, body.label, body.workflow)
        s = request.app.state.services.cfg.settings
        comfy = next((pid for pid, x in (s.providers if s else {}).items()
                      if x.get("kind") == "comfyui"), None)
        changes = None
        if comfy is not None:
            providers = {**s.providers, comfy: {**s.providers[comfy], "models": {
                **(s.providers[comfy].get("models") or {}),
                p.id: {"label": p.label, "caps": [p.role]}}}}
            role = s.roles.get(p.role) or {}
            roles = {**s.roles, p.role: {**role, "chain": [*(role.get("chain") or []),
                                                           f"{comfy}/{p.id}"]}}
            changes = {"providers": providers, "roles": roles}
        return await _saved(request, p, changes)


@router.put("/generation-presets/{pid}")
async def edit_preset(request: Request, pid: str, body: ValuesIn) -> dict:
    p = _preset(pid)
    data, wf = presets_mod.edited(p, body.values)
    presets_mod.save(p.kind, p.id, data, wf, "own" if p.origin == "own" else "edited")
    return await _saved(request, p)


@router.put("/generation-presets/{pid}/workflow")
async def replace_workflow(request: Request, pid: str, body: WorkflowIn) -> dict:
    p = _preset(pid)
    presets_mod.replace_workflow(p, body.workflow)
    return await _saved(request, p)


@router.get("/generation-presets/{pid}/workflow")
async def download_workflow(pid: str) -> Response:
    p = _preset(pid)
    return Response(json.dumps(presets_mod.with_placeholders(p), indent=2,
                               ensure_ascii=False) + "\n",
                    media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{p.id}.json"'})


@router.post("/generation-presets/{pid}/reset")
async def reset_preset(request: Request, pid: str) -> dict:
    p = _preset(pid)
    if p.origin != "edited":
        raise CelineError("bad_request", f"{p.label} is as shipped.", 400)
    presets_mod.forget(p.kind, p.id)
    return await _saved(request, p)


@router.delete("/generation-presets/{pid}")
async def delete_preset(request: Request, pid: str) -> dict:
    p = _preset(pid)
    if p.origin != "own":
        raise CelineError("bad_request", f"{p.label} is shipped: Reset to defaults "
                          f"undoes its changes.", 400)
    async with write.lock():
        s = request.app.state.services.cfg.settings
        presets_mod.forget(p.kind, p.id)
        providers = {pid_: {**x, "models": {k: v for k, v in (x.get("models") or {}).items()
                                           if not (x.get("kind") == "comfyui" and k == p.id)}}
                     for pid_, x in s.providers.items()}
        roles = {r: {**v, "chain": [ref for ref in v.get("chain") or []
                                    if not (s.split(ref)[1] == p.id
                                            and s.providers.get(s.split(ref)[0], {}).get(
                                                "kind") == "comfyui")]}
                 for r, v in s.roles.items()}
        await reload.commit(request.app.state.services, {"providers": providers, "roles": roles})
        write.publish(request.app.state.services, ["presets"])
    return {"deleted": True}
