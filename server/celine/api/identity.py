"""People across sources over HTTP: the People page."""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..errors import CelineError
from ..kb import identity
from .deps import in_thread

router = APIRouter(prefix="/api")


class LinkBody(BaseModel):
    refs: list[str] = Field(min_length=2)
    name: str = Field(min_length=1, max_length=200)


class PersonBody(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    about: str | None = Field(default=None, max_length=identity.ABOUT_MAX)
    aliases: list[str] | None = Field(default=None, max_length=identity.ALIASES_MAX)


def _bad(e: Exception) -> CelineError:
    return CelineError("bad_request", str(e), 400)


@router.get("/people")
async def people(request: Request, q: str = "", kb: str | None = None,
                 limit: int = 500) -> dict:
    """One entry per person from every chat source (the people lists' rule)."""
    ids = await identity.snapshot(request.app.state.services.store)
    rows = await in_thread(identity.people, request.app.state.services.kbs, ids, q,
                           kb or None, max(1, min(limit, 2000)))
    return {"people": rows}


@router.post("/people")
async def link(request: Request, body: LinkBody) -> dict:
    """A same person of these accounts (refs), named."""
    try:
        out = await request.app.state.services.store.write(
            lambda c: identity.link_sync(c, body.refs, name=body.name))
    except identity.IdentityError as e:
        raise _bad(e) from None
    return {"person": out}


@router.delete("/people/{pid}/link")
async def unlink(request: Request, pid: str) -> dict:
    """Account `pid` (a ref) leaves its same person."""
    try:
        return await request.app.state.services.store.write(lambda c: identity.unlink_sync(c, pid))
    except identity.IdentityError as e:
        raise _bad(e) from None


@router.patch("/people/{pid}")
async def set_person(request: Request, pid: str, body: PersonBody) -> dict:
    """The person of account `pid` (a ref, "discord:7"): a name (a same person only),
    about text and aliases."""
    try:
        out = await request.app.state.services.store.write(lambda c: identity.set_person_sync(
            c, pid, name=body.name, about=body.about, aliases=body.aliases))
    except identity.IdentityError as e:
        raise _bad(e) from None
    return {"person": out}
