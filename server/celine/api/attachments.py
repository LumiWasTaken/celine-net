"""Attachments over HTTP: upload a draft, drop a draft, serve a stored file."""
from __future__ import annotations

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse

from .. import attachments, box_files
from ..errors import NotFound

router = APIRouter(prefix="/api")


@router.post("/attachments")
async def upload_attachment(request: Request, file: UploadFile = File(...)) -> dict:
    cfg, store = request.app.state.services.cfg, request.app.state.services.store
    row = await attachments.save(cfg, store, file)
    box_files.copy_later(cfg, attachments.blob_path(cfg, row["sha256"]), row["filename"], store,
                         row["id"])
    return row


@router.delete("/attachments/{aid}")
async def delete_attachment(request: Request, aid: str) -> dict:
    return {"removed": await attachments.delete_draft(request.app.state.services.store, aid)}


# the browser may render these, so nothing uploaded can run on this origin: only raster
# images and audio are shown inline, the rest is a download, and a sandbox covers both
SAFE_FILE_HEADERS = {"X-Content-Type-Options": "nosniff",
                     "Content-Security-Policy": "default-src 'none'; sandbox"}


def shown_inline(media_type: str) -> bool:
    return (media_type.startswith("image/") and media_type != "image/svg+xml") or media_type.startswith("audio/")


@router.get("/attachments/{aid}/file")
async def attachment_file(request: Request, aid: str, download: bool = False):
    """The file under its own name and type; `download` saves even an image."""
    row = await attachments.get(request.app.state.services.store, aid)
    path = attachments.blob_path(request.app.state.services.cfg, row["sha256"]) if row else None
    if not row or not path.exists():
        raise NotFound("attachment")
    inline = shown_inline(row["media_type"]) and not download
    return FileResponse(path, media_type=row["media_type"], filename=row["filename"],
                        content_disposition_type="inline" if inline else "attachment",
                        headers={**SAFE_FILE_HEADERS,
                                 "Cache-Control": "private, max-age=604800, immutable"})
