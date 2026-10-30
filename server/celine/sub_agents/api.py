"""The agent's sub-agents over HTTP: the overview, start one, stop one. The same code its
tools run (skill.py begin, stop_agent), so the API and it see one thing."""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from ..errors import CelineError, NotFound
from .skill import agents_of, labelled, tree_of

router = APIRouter(prefix="/api")


class AgentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    brief: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=80)
    access: str | None = None        # read | work
    folder: str | None = None
    model: str | None = None         # one of its models (settings/her_models.py)
    cli: str | None = None           # an agent CLI in its shellbox (cli.py CLIS)
    device: str | None = None        # a device it runs on, a drone (drone.py devices)


def _skill(request: Request):
    reg = request.app.state.services.registry
    skill = reg.skills.get("sub_agents") if reg is not None else None
    if skill is None:
        raise CelineError("unavailable", "Sub-agents are switched off.", 409)
    return skill


@router.get("/sub-agents")
async def list_agents(request: Request, conversation: str | None = None) -> dict:
    """The sub-agents of one conversation, or every one not in the archive, newest
    first: id, parent, name, access, folder, model, cli, device, working, the step at
    work, the latest report, its last words."""
    services = request.app.state.services
    rows = await services.store.read(lambda c: agents_of(c, conversation, own=services.store.tool_words))
    return {"agents": labelled(getattr(services.cfg, "settings", None), rows)}


@router.get("/conversations/{cid}/sub-agents")
async def agent_tree(request: Request, cid: str) -> dict:
    """The sub-agents under the top of `cid`'s chain, as a tree (skill.tree_of): {top,
    agents}, each with its depth, state, model (its label), tokens, seconds and link."""
    services = request.app.state.services
    out = await services.store.read(lambda c: tree_of(c, cid, services.store.tool_words))
    if out is None:
        raise NotFound("conversation")
    return {**out, "agents": labelled(getattr(services.cfg, "settings", None), out["agents"])}


@router.post("/conversations/{cid}/sub-agents")
async def start_agent(request: Request, cid: str, body: AgentIn) -> dict:
    """Start a sub-agent of conversation `cid` with a brief, like its
    sub_agents__start: the name of one that is done gives it the brief (resumed). The
    deepest sub-agent starts none (skill.MAX_DEPTH)."""
    store = request.app.state.services.store
    if await store.read(lambda c: c.execute(
            "SELECT 1 FROM conversations WHERE id=?", (cid,)).fetchone()) is None:
        raise NotFound("conversation")
    sub, refusal, resumed, _ = await _skill(request).begin(
        store, cid, body.brief, body.name, body.access, body.folder, body.model, body.cli,
        body.device)
    if refusal:
        raise CelineError("refused", refusal, 409)
    return {"conversation_id": sub, "resumed": resumed}


@router.post("/sub-agents/{sub}/stop")
async def stop_agent(request: Request, sub: str) -> dict:
    """Stop a sub-agent: its turn, its jobs, and what it scheduled."""
    store = request.app.state.services.store
    if await store.read(lambda c: c.execute(
            "SELECT 1 FROM conversations WHERE id=? AND agent_of IS NOT NULL",
            (sub,)).fetchone()) is None:
        raise NotFound("agent")
    await _skill(request).stop_agent(sub)
    return {"stopped": True}
