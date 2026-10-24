"""A thin speech HTTP client. No engines, model downloads or browser credentials."""
from __future__ import annotations

import json

import httpx

from .errors import CelineError
from .settings import secrets
from .settings.speech import secret

MAX_RECORDING = 12_000_000
MAX_AUDIO = 20_000_000
MAX_TEXT = 900
TIMEOUT = 180.0


def endpoint(cfg, direction: str) -> tuple[str, dict, str]:
    s = cfg.settings
    value = s.speech.get(direction, {})
    if not value.get("url") or not value.get("model"):
        raise CelineError("unavailable", "Set up the speech provider in Settings, Features.", 503)
    key = secrets.read_key(s.secrets_root, secret(direction)) if s.secrets_root else None
    base = value["url"].rstrip("/")
    return base, {"Authorization": f"Bearer {key}"} if key else {}, value["model"]


def client():
    return httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False)


async def call(cfg, direction: str, path: str, *, limit: int, **kw) -> tuple[bytes, str]:
    base, headers, _ = endpoint(cfg, direction)
    try:
        async with client() as c:
            async with c.stream(kw.pop("method", "POST"), base + path, headers=headers, **kw) as r:
                # Never forward error bodies or follow redirects: either can expose a key.
                if not 200 <= r.status_code < 300:
                    raise CelineError("speech_provider", f"Speech provider returned HTTP {r.status_code}.", 502)
                chunks, size = [], 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise CelineError("speech_provider", "Speech provider response is too large.", 502)
                    chunks.append(chunk)
                return b"".join(chunks), r.headers.get("content-type", "").split(";")[0].strip().lower()
    except httpx.HTTPError:
        raise CelineError("speech_provider", "The speech provider could not be reached.", 502) from None


async def transcribe(cfg, audio: bytes, mime: str, language: str) -> dict:
    _, _, model = endpoint(cfg, "input")
    extensions = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a",
                  "audio/wav": "wav", "audio/mpeg": "mp3"}
    mime = mime.split(";")[0].strip().lower()
    if mime not in extensions or not audio or len(audio) > MAX_RECORDING:
        raise CelineError("bad_request", "Use a nonempty audio recording smaller than 12 MB.", 400)
    fields = {"model": model, "response_format": "json"}
    if language:
        fields["language"] = language
    body, _ = await call(cfg, "input", "/audio/transcriptions", limit=1_000_000,
                         data=fields, files={"file": (f"recording.{extensions[mime]}", audio, mime)})
    try:
        result = json.loads(body)
    except (ValueError, UnicodeError):
        result = None
    if not isinstance(result, dict) or not isinstance(result.get("text"), str):
        raise CelineError("speech_provider", "Speech provider returned no transcript.", 502)
    return {"text": result["text"].strip()}


async def synthesize(cfg, text: str, voice: str) -> tuple[bytes, str]:
    _, _, model = endpoint(cfg, "output")
    body, mime = await call(cfg, "output", "/audio/speech", limit=MAX_AUDIO,
                            json={"model": model, "input": text, "voice": voice or "default",
                                  "response_format": "wav"})
    if not body or not mime.startswith("audio/"):
        raise CelineError("speech_provider", "Speech provider returned no audio.", 502)
    return body, mime


async def voices(cfg) -> dict:
    body, _ = await call(cfg, "output", "/voices", method="GET", limit=1_000_000)
    try:
        result = json.loads(body)
    except (ValueError, UnicodeError):
        result = None
    raw = result.get("voices") if isinstance(result, dict) else None
    if not isinstance(raw, list):
        raise CelineError("speech_provider", "Speech provider returned no voice list.", 502)
    ids = [v if isinstance(v, str) else v.get("id") if isinstance(v, dict) else None for v in raw]
    if any(not isinstance(v, str) or not v or len(v) > 200 for v in ids):
        raise CelineError("speech_provider", "Speech provider returned an invalid voice list.", 502)
    return {"voices": list(dict.fromkeys(ids))}
