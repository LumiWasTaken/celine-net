"""Speech configuration and fixed upstream routes, for the logged-in person only."""
from __future__ import annotations

import copy

from fastapi import APIRouter, File, Form, Header, Request, Response, UploadFile

from .. import reload, speech
from ..errors import CelineError, NotFound
from ..settings import features, files, secrets, write
from ..settings import speech as settings
from ..settings.files import Invalid, check_version, version
from .deps import cfg_of, etag, json_body, person_only, services_of

router = APIRouter(prefix="/api")


def view(cfg, direction: str) -> dict:
    if direction not in settings.DIRECTIONS:
        raise NotFound("speech settings")
    s = cfg.settings
    value = s.speech.get(direction, {})
    return {"endpoint": value, "version": version(value), "fields": settings.FIELDS,
            "key": secrets.state(s.secrets_root, settings.secret(direction))}


@router.get("/speech/{direction}")
async def get_settings(request: Request, response: Response, direction: str) -> dict:
    person_only(request)
    result = view(cfg_of(request), direction)
    etag(response, result["version"])
    return result


@router.put("/speech/{direction}")
async def put_settings(request: Request, response: Response, direction: str,
                       if_match: str | None = Header(default=None)) -> dict:
    person_only(request)
    cfg = cfg_of(request)
    view(cfg, direction)
    body = await json_body(request, need=("endpoint",), allow=("endpoint", "key"))
    key = body.get("key")
    if key is not None and (not isinstance(key, str) or len(key) > secrets.MAX_KEY
                            or any(c in key for c in "\r\n\0")):
        raise Invalid("key", "That does not look like an API key.")
    async with write.lock():
        s = cfg.settings
        before = s.speech.get(direction, {})
        check_version("speech", before, if_match)
        clean = settings.validate({direction: body["endpoint"]})[direction]
        root, name = s.secrets_root, settings.secret(direction)
        previous_key = secrets.read_key(root, name)
        replacing = isinstance(key, str) and bool(key.strip())
        clearing = "key" in body and key is None
        if (clean.get("url") != before.get("url") and previous_key
                and not replacing and not clearing):
            raise Invalid("key", "Replace or clear the saved key when changing the provider address.")
        # Blank input preserves a key; only explicit null clears it.
        if replacing:
            secrets.set_key(root, name, key)
        elif clearing:
            secrets.delete_key(root, name)
        changed = copy.deepcopy(s.speech)
        changed[direction] = clean
        try:
            out = await reload.commit(services_of(request), {"speech": changed})
        except BaseException:
            if replacing or clearing:
                disk, error = files.read(files.settings_dir(cfg), "speech")
                disk_url = (disk or {}).get(direction, {}).get("url")
                live_url = cfg.settings.speech.get(direction, {}).get("url")
                # A commit may fail after writing or applying the new address.
                # Never restore the old address's key beside a changed address.
                if previous_key and not error and disk_url == live_url == before.get("url"):
                    secrets.set_key(root, name, previous_key)
                else:
                    secrets.delete_key(root, name)
            raise
        result = view(cfg, direction)
        etag(response, result["version"])
        return {**result, **out}


def allowed(request: Request, direction: str):
    person_only(request)
    cfg = cfg_of(request)
    if not features.on(cfg, f"speech_{direction}"):
        raise CelineError("unavailable", "This speech feature is switched off.", 409)
    return cfg


@router.get("/speech/output/voices")
async def list_voices(request: Request) -> dict:
    # Explicit discovery also works before enabling playback. It tests only this route.
    person_only(request)
    return await speech.voices(cfg_of(request))


@router.get("/speech/{direction}/models")
async def list_models(request: Request, direction: str) -> dict:
    person_only(request)
    cfg = cfg_of(request)
    view(cfg, direction)
    return await speech.models(cfg, direction)


@router.post("/transcriptions")
@router.post("/speech/input/tests")
async def transcribe(request: Request, file: UploadFile = File(...), language: str = Form("")) -> dict:
    person_only(request)
    cfg = cfg_of(request) if request.url.path.endswith("/tests") else allowed(request, "input")
    if len(language) > 32 or any(not (c.isalpha() or c == "-") for c in language):
        raise Invalid("language", "Use a language code, or leave it empty.")
    try:
        audio = await file.read(speech.MAX_RECORDING + 1)
        return await speech.transcribe(cfg, audio, file.content_type or "", language)
    finally:
        await file.close()


@router.post("/speech/audio")
@router.post("/speech/output/tests")
async def synthesize(request: Request) -> Response:
    person_only(request)
    cfg = cfg_of(request) if request.url.path.endswith("/tests") else allowed(request, "output")
    body = await json_body(request, need=("text",), allow=("text", "voice"))
    text, voice = body["text"], body.get("voice", "")
    if not isinstance(text, str) or not text.strip() or len(text) > speech.MAX_TEXT:
        raise Invalid("text", "Speech takes 1 to 900 characters at a time.")
    if not isinstance(voice, str) or len(voice) > 200 or any(c in voice for c in "\r\n\0"):
        raise Invalid("voice", "Use a voice name.")
    audio, mime = await speech.synthesize(cfg, text, voice)
    return Response(audio, media_type=mime, headers={"Cache-Control": "no-store",
                                                  "X-Content-Type-Options": "nosniff"})
