"""Settings, Prompts (settings/prompts.py) and a conversation's context blocks as text.

GET /api/prompts: the groups of every text a model reads, with `help`. GET and PUT
/api/prompts/{id}: one text; PUT {text} saves the user's edit, {text: null} or the shipped
text goes back to the shipped one (Use the default); a text that would break its reader
is 400 (settings/prompts.py `problem`). Each save is kept in its History (texts.py
`prompt:<id>`), drops the text's "nothing fills" Problem (celine/prompts.py `settled`)
and sends `settings.changed` (what: ["prompts"]). An id that is no text is 404, so no path
reaches outside the catalog.

GET /api/conversations/{cid}/context/{block}: one block of the Context tab as the next
request of that conversation would carry it now ({name, text}): the system prompt's piece
of that name, the tool schemas after the skill index for `tools`, the messages for
`history` (turns/prompt.py turn_parts, turns/context.py in mode "dry")."""
from __future__ import annotations

import json
import tomllib

from fastapi import APIRouter, Request

from .. import agent, prompts as package_texts
from ..errors import NotFound
from ..guard import persona as bastet
from ..settings import prompts, voice, write
from .deps import json_body, services_of

router = APIRouter(prefix="/api")


@router.get("/prompts")
async def list_prompts(request: Request) -> dict:
    cfg = services_of(request).cfg
    own = await services_of(request).store.read(lambda c: _own(cfg, c))
    return {"groups": prompts.catalog(cfg.substitute, own), "help": prompts.HELP}


def _own(cfg, c) -> set[str]:
    """The rows of Settings, Prompts edited on their own page (settings/prompts.py
    ELSEWHERE) whose text is not the shipped one: the her row, Bastet's texts."""
    out = {f"bastet.{f}" for f, v in bastet.view(c).items() if v["own"]}
    h = cfg.agent
    if h is not None:
        shipped_voice = voice.validate(tomllib.loads((agent.BASE / "voice.toml").read_text()))
        out |= {pid for pid, own in (
            ("agent.persona", h.persona.strip() != agent.base_persona().strip()),
            ("agent.talk", h.talk is not None),
            ("agent.banned", agent.voice_text(h.voice) != agent.voice_text(shipped_voice))) if own}
    return out


def _known(pid: str) -> str:
    if pid not in prompts.editable():
        raise NotFound("prompt")
    return pid


@router.get("/prompts/{pid}")
async def get_prompt(request: Request, pid: str) -> dict:
    return prompts.view(_known(pid), services_of(request).cfg.substitute)


@router.put("/prompts/{pid}")
async def put_prompt(request: Request, pid: str) -> dict:
    _known(pid)
    body = await json_body(request, need=("text",), allow=("text",))
    text = body["text"]
    services = services_of(request)
    async with write.lock():
        await services.store.write(lambda c: prompts.save(c, pid, text))
    package_texts.settled(services.cfg, pid)
    write.publish(services, ["prompts"])
    return prompts.view(pid, services.cfg.substitute)


BLOCKS = ("persona", "talk", "about", "notes", "tools", "history")


@router.get("/conversations/{cid}/context/{block}")
async def context_block(request: Request, cid: str, block: str) -> dict:
    from ..turns import context
    from ..turns.prompt import turn_parts
    if block not in BLOCKS:
        raise NotFound("block")
    services = services_of(request)
    exists = await services.store.read(lambda c: c.execute(
        "SELECT 1 FROM conversations WHERE id=?", (cid,)).fetchone())
    if exists is None:
        raise NotFound("chat")
    backend, pieces, tools, _ = await turn_parts(services, cid)
    parts = [t for name, t in pieces if name == block]
    if block == "tools" and tools:
        parts.append(json.dumps(tools, indent=1, ensure_ascii=False))
    if block == "history":
        built = await context.build(services.store, services.cfg, backend, cid,
                                    system="\n\n".join(t for _, t in pieces), tools=tools,
                                    mode="dry")
        parts = [f"{m.get('role')}:\n{_content(m.get('content'))}" for m in built.messages[1:]]
    return {"name": block, "text": "\n\n".join(parts)}


def _content(content) -> str:
    return content if isinstance(content, str) else json.dumps(content, indent=1, ensure_ascii=False)
