"""The agent (celine/her.py): GET and PUT /api/agent, its portrait. A write reloads `cfg.her` and
sends `her.changed` on the global feed."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import FileResponse

from .. import her
from ..errors import NotFound
from ..settings import files
from ..settings.files import Invalid, version
from ..settings import write
from .deps import cfg_of, etag, json_body, store_of
from ..core.event_types import HER_CHANGED

router = APIRouter(prefix="/api")
log = logging.getLogger("celine.her")


async def _changed(request: Request) -> None:
    await her.refresh(store_of(request), cfg_of(request))
    store_of(request).emit_global({"type": HER_CHANGED})


@router.get("/agent")
async def get_her(request: Request, response: Response) -> dict:
    """The agent's plain fields, persona, talk (null: the shipped one, which is `talk_shipped`),
    voice and `voice_text` (as its History keeps it), its portrait, plus `version`."""
    out = her.package(cfg_of(request))
    return {**out, "version": etag(response, version(out))}


@router.put("/agent")
async def put_her(request: Request, response: Response,
                  if_match: str | None = Header(default=None)) -> dict:
    """Any of: name, pronouns, color, persona, talk (null: back to the shipped one),
    voice (settings/voice.py). Checked whole before anything is written."""
    body = await json_body(request)
    async with write.lock():
        cfg = cfg_of(request)
        files.check_version("her", her.package(cfg), if_match)
        for k in ("persona", "talk"):
            if isinstance(body.get(k), str):
                body[k] = her.stored(cfg, cfg.her, body[k])
        await her.write(store_of(request), cfg, body)
        await _changed(request)
    return await get_her(request, response)


@router.put("/agent/portrait")
async def put_portrait(request: Request) -> dict:
    """The agent's portrait: multipart with one file `image` (png, jpg or webp, at most the
    attachment limit), or JSON {attachment} (an attachment's id: a picture it made, one
    the user sent). -> {portrait: {url}}."""
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        image = form.get("image")
        if image is None or isinstance(image, str):
            raise Invalid("image", "'image' is missing.")
        limit = int(cfg_of(request).r("max_attachment_bytes"))
        data = await image.read(limit + 1)
        if len(data) > limit:
            raise Invalid("image", f"The image is larger than {limit:,} bytes.")
        field = "image"
    else:
        from ..attachments import blob_path, get
        body = await json_body(request, need=("attachment",), allow=("attachment",))
        row = await get(store_of(request), str(body["attachment"]))
        if row is None or row["kind"] != "image":
            raise NotFound("picture")
        try:
            data = blob_path(cfg_of(request), row["sha256"]).read_bytes()
        except OSError:
            raise NotFound("picture") from None
        field = "attachment"
    try:
        await her.set_portrait(store_of(request), cfg_of(request), data)
    except ValueError as e:
        raise Invalid(field, str(e)) from None
    await _changed(request)
    return {"portrait": her.portrait_view(cfg_of(request).her)}


@router.delete("/agent/portrait")
async def delete_portrait(request: Request) -> dict:
    """{deleted}: false when there was none."""
    if not (cfg_of(request).her and cfg_of(request).her.portrait):
        return {"deleted": False}
    await her.set_portrait(store_of(request), cfg_of(request), None)
    await _changed(request)
    return {"deleted": True}


@router.get("/agent/portrait", response_model=None)
async def get_portrait(request: Request) -> FileResponse | Response:
    """The agent's portrait; 204 when there is none (the web shows its initial)."""
    got = her.portrait_file(cfg_of(request))
    if got is None:
        return Response(status_code=204, headers={"Cache-Control": "no-cache"})
    return FileResponse(got[0], media_type=got[1], headers={"Cache-Control": "no-cache"})


@router.get("/agent/mood")
async def mood(request: Request) -> dict:
    """The agent's mood next to its name (skills/mind.py mind__set_status): {text, set_at}, null until
    it set one; every change is the global event `mood`."""
    from ..skills.mind import get_mood
    return {"mood": await get_mood(store_of(request))}
