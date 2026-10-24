"""The skills catalog, a dead MCP server's retry, and a conversation's tool switches."""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..errors import CelineError, NotFound
from ..settings.registry import TARGET
from ..skills.catalog import catalog
from ..skills.gating import overrides_for, set_override

router = APIRouter(prefix="/api")


@router.get("/skills")
async def skills(request: Request, conversation: str | None = None) -> dict:
    """The catalog; with `conversation`, only what its turns can get there (the skills
    offered in its kind: turns/loads.offered). A disabled tool is still listed. Per
    tool: `label` and `summary` for you (never the model-facing docstring), `enabled`
    here, `default` (Settings), and `why` ("here" = this conversation's switch,
    "default")."""
    from ..turns import loads
    reg = request.app.state.services.registry
    store = request.app.state.services.store
    over = await overrides_for(store, conversation) if conversation else {}
    here = None
    if conversation:
        here = {s.wire_name for specs in (await loads.offered(reg, store, conversation))
                .values() for s in specs}
    return {"skills": catalog(request.app.state.services, over, here)}


@router.post("/skills/{name}/restart")
async def retry_skill(request: Request, name: str) -> dict:
    """Restart a dead MCP server."""
    if not request.app.state.services.mcp.retry(name):
        raise NotFound("MCP server")
    return {"ok": True}


class Override(BaseModel):
    enabled: bool
    client_seq: int = 0


def _target(target: str) -> str:
    if not TARGET.match(target):
        raise CelineError("bad_request", "A target is a skill (chat_archive) or a tool "
                                         "(chat_archive__search).", 400)
    return target


@router.put("/conversations/{cid}/tool-settings/{target}")
async def set_tool(request: Request, cid: str, target: str, body: Override) -> dict:
    try:
        seq = await set_override(request.app.state.services.store, cid, _target(target),
                                 body.enabled, body.client_seq)
    except KeyError:
        raise NotFound("conversation") from None
    return {"seq": seq, "applied": seq is not None}
