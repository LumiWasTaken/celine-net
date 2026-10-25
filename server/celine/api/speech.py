"""Speech for the logged-in person: a recording to text (the Dictation role's model), a
reply's text to audio and the voices (the Read aloud role's). The feature switches only
show or hide the controls: a test before switching one on goes through the same routes."""
from __future__ import annotations

from collections.abc import Awaitable

import anyio
from fastapi import APIRouter, Request, Response
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from .. import speech
from ..errors import CelineError
from ..settings.files import Invalid
from .deps import cfg_of, json_body, person_only

router = APIRouter(prefix="/api")


async def connected[T](request: Request, operation: Awaitable[T]) -> T:
    """After reading the request body, stop provider work when the caller leaves."""
    error = None
    async with anyio.create_task_group() as tg:
        async def watch():
            while (await request.receive())["type"] != "http.disconnect":
                pass
            tg.cancel_scope.cancel()
        tg.start_soon(watch)
        try:
            return await operation
        except Exception as exc:
            # Raise outside the task group so the API's normal error handler sees it,
            # not an ExceptionGroup. Cancellation still unwinds the HTTP client.
            error = exc
        finally:
            tg.cancel_scope.cancel()
    if error is not None:
        raise error
    raise CelineError("cancelled", "The speech request was cancelled.", 499)


@router.get("/speech/voices")
async def list_voices(request: Request) -> dict:
    person_only(request)
    return await connected(request, speech.voices(cfg_of(request)))


@router.post("/speech/transcriptions")
async def transcribe(request: Request) -> dict:
    person_only(request)
    cfg = cfg_of(request)
    # Count actual bytes before feeding the parser, including multipart overhead.
    # Content-Length is only an early rejection, never the source of truth.
    limit = speech.MAX_RECORDING + 64 * 1024
    too_large = CelineError("too_large", "The recording upload is too large.", 413)
    length = request.headers.get("content-length")
    if length is not None:
        try:
            size = int(length)
        except ValueError:
            raise Invalid("file", "Use a valid upload length.") from None
        if size < 0:
            raise Invalid("file", "Use a valid upload length.")
        if size > limit:
            raise too_large
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise Invalid("file", "Upload a recording as multipart form data.")

    async def bounded():
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                # MultiPartParser closes partial temporary files on stream errors.
                raise too_large
            yield chunk

    try:
        form = await MultiPartParser(request.headers, bounded(), max_files=1,
                                     max_fields=1, max_part_size=128).parse()
    except MultiPartException:
        raise Invalid("file", "Use one recording and an optional language code.") from None
    try:
        file, language = form.get("file"), form.get("language", "")
        if not isinstance(file, UploadFile) or any(name not in ("file", "language") for name in form):
            raise Invalid("file", "Upload one recording.")
        if (not isinstance(language, str) or len(language) > 32
                or any(not (c.isalpha() or c == "-") for c in language)):
            raise Invalid("language", "Use a language code, or leave it empty.")
        audio = await file.read(speech.MAX_RECORDING + 1)
        return await connected(request, speech.transcribe(cfg, audio, file.content_type or "", language))
    finally:
        await form.close()


@router.post("/speech/clips")
async def synthesize(request: Request) -> Response:
    person_only(request)
    cfg = cfg_of(request)
    body = await json_body(request, need=("text",), allow=("text", "voice"))
    text, voice = body["text"], body.get("voice", "")
    if not isinstance(text, str) or not text.strip() or len(text) > speech.MAX_TEXT:
        raise Invalid("text", "Speech takes 1 to 900 characters at a time.")
    if not isinstance(voice, str) or len(voice) > 200 or any(c in voice for c in "\r\n\0"):
        raise Invalid("voice", "Use a voice name.")
    audio, mime = await connected(request, speech.synthesize(cfg, text, voice))
    return Response(audio, media_type=mime, headers={"Cache-Control": "no-store",
                                                  "X-Content-Type-Options": "nosniff"})
