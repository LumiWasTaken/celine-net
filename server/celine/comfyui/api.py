"""Images and music over HTTP, the same batches its tools make (batches.py):

- GET /api/generation-presets?kind=images|music: every preset of the kind as the UI
  shows it (presets.view: its editor's fields and values, `origin` shipped, edited or
  own), with `ref` ("provider/preset" in its role, null when the role does not list it)
  and `order` (its place in the role, null likewise);
  what changes them is api/presets.py;
- GET /api/batches?kind=&conversation=&limit=: newest first;
- GET /api/batches/{id};
- POST /api/batches {kind, items, count?}: the user's batch, no conversation; it runs
  in the app and returns at once, its slots filled by the events;
- POST /api/batches/{id}/items/{index}/retry, POST /api/batches/{id}/stop -> {stopped}.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..errors import CelineError, NotFound
from . import batches as batches_mod, presets as presets_mod

router = APIRouter(prefix="/api")


class ImageItemIn(BaseModel):
    prompt: str = Field(min_length=1)
    source: str | None = None          # an image attachment's id: an edit
    preset: str | None = None
    negative: str | None = None
    width: int | None = None
    height: int | None = None
    seed: int | None = None
    model: str | None = None
    loras: list[str] | None = None
    mode: str | None = None


class MusicItemIn(BaseModel):
    style: str = Field(min_length=1)
    lyrics: str | None = None
    title: str | None = None
    instrumental: bool = True
    duration: float | None = None
    seed: int | None = None


class ImagesIn(BaseModel):
    kind: Literal["images"]
    items: list[ImageItemIn] = Field(min_length=1)
    count: int = Field(default=1, ge=1)


class MusicIn(BaseModel):
    kind: Literal["music"]
    items: list[MusicItemIn] = Field(min_length=1)
    count: int = Field(default=1, ge=1)


BatchIn = Annotated[ImagesIn | MusicIn, Field(discriminator="kind")]
KINDS = ("images", "music")


def _kind(kind: str) -> str:
    if kind not in KINDS:
        raise CelineError("bad_request", "kind is images or music.", 400)
    return kind


async def _batch(request: Request, bid: str) -> dict:
    b = await batches_mod.get(request.app.state.services.store, bid)
    if b is None:
        raise NotFound("batch")
    return b


def _runner(request: Request):
    runner = request.app.state.services.batches
    if runner is None or request.app.state.services.llm is None:
        raise CelineError("unavailable", "ComfyUI is not running in this app.", 409)
    return runner


@router.get("/generation-presets")
async def list_presets(request: Request, kind: str) -> dict:
    return {"presets": presets_mod.listing(request.app.state.services.cfg.settings, _kind(kind))}


@router.get("/batches")
async def list_batches(request: Request, kind: str, conversation: str | None = None,
                       limit: int = 50) -> dict:
    rows = await batches_mod.list_batches(request.app.state.services.store, _kind(kind),
                                          conversation, max(1, min(limit, 500)))
    return {"batches": [batches_mod.view(b) for b in rows]}


@router.get("/batches/{bid}")
async def get_batch(request: Request, bid: str) -> dict:
    return batches_mod.view(await _batch(request, bid))


@router.post("/batches")
async def start_batch(request: Request, body: BatchIn) -> dict:
    runner = _runner(request)
    items = [i.model_dump(exclude_none=True) for i in body.items]
    try:
        b = await runner.create(body.kind, items, count=body.count)
    except batches_mod.TooMany as e:
        raise CelineError("bad_request", str(e), 400) from None
    if b["state"] == "running":
        runner.start(b["id"])
    return batches_mod.view(b)


@router.post("/batches/{bid}/items/{index}/retry")
async def retry_item(request: Request, bid: str, index: int) -> dict:
    await _batch(request, bid)
    try:
        return batches_mod.view(await _runner(request).retry(bid, index))
    except batches_mod.ImageError as e:
        raise CelineError("conflict", str(e), 409) from None


@router.post("/batches/{bid}/stop")
async def stop_batch(request: Request, bid: str) -> dict:
    await _batch(request, bid)
    return {"stopped": await _runner(request).stop(bid)}
