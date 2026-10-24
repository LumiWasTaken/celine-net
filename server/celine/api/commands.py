"""Slash commands over HTTP: the list and a run (the registry: commands/)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .. import commands
from ..conversations.store import ensure_writable

router = APIRouter(prefix="/api")


class RunBody(BaseModel):
    text: str = ""                   # everything after the command name, as typed


@router.get("/slash-commands")     # /terminal-commands is the terminals' log (terminals/api.py)
async def list_commands(request: Request, conversation: str | None = None) -> dict:
    """With ?conversation=<id>, only the commands of its kind, and arguments carry
    `suggestions` for that conversation (the thinking words of its current model)."""
    return {"commands": await commands.listing(request.app.state.services, conversation)}


@router.post("/conversations/{cid}/slash-commands/{name}/run")
async def run_command(request: Request, cid: str, name: str, body: RunBody | None = None,
                      wait: bool = False) -> dict:
    services = request.app.state.services
    await ensure_writable(services.store, cid)
    return await commands.run(services, cid, name, body.text if body else "", wait=wait)
