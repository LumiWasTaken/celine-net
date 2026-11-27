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
    relation: str | None = Field(default=None, max_length=identity.RELATION_MAX)
    aliases: list[str] | None = Field(default=None, max_length=identity.ALIASES_MAX)


class NoteBody(BaseModel):
    text: str = Field(min_length=1, max_length=identity.NOTE_MAX)


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


@router.get("/people/{pid}")
async def person(request: Request, pid: str) -> dict:
    """One person in full: relation, aliases, notes, and every account with its profile
    and the names it had."""
    s = request.app.state.services
    ids = await identity.snapshot(s.store)
    try:
        out = await in_thread(identity.person, s.kbs, ids, pid)
    except identity.IdentityError as e:
        raise _bad(e) from None
    if out is None:
        raise CelineError("not_found", f"There is no person {pid}.", 404)
    return {"person": out}


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
    """The person of account `pid` (a ref, "discord:person:7"): a name (a same person
    only), how they relate to you, and aliases."""
    try:
        out = await request.app.state.services.store.write(lambda c: identity.set_person_sync(
            c, pid, name=body.name, relation=body.relation, aliases=body.aliases))
    except identity.IdentityError as e:
        raise _bad(e) from None
    return {"person": out}


@router.post("/people/{pid}/notes")
async def add_note(request: Request, pid: str, body: NoteBody) -> dict:
    """A note of yours about the person of account `pid`."""
    try:
        out = await request.app.state.services.store.write(
            lambda c: identity.note_sync(c, pid, body.text))
    except identity.IdentityError as e:
        raise _bad(e) from None
    return {"person": out}


@router.patch("/people/{pid}/notes/{note_id}")
async def change_note(request: Request, pid: str, note_id: int, body: NoteBody) -> dict:
    """A note about a person, reworded. Whoever wrote it first, it is yours now."""
    try:
        out = await request.app.state.services.store.write(
            lambda c: identity.change_note_sync(c, note_id, body.text))
    except identity.IdentityError as e:
        raise _bad(e) from None
    return {"person": out}


@router.delete("/people/{pid}/notes/{note_id}")
async def remove_note(request: Request, pid: str, note_id: int) -> dict:
    try:
        out = await request.app.state.services.store.write(
            lambda c: identity.change_note_sync(c, note_id, None))
    except identity.IdentityError as e:
        raise _bad(e) from None
    return {"person": out}
