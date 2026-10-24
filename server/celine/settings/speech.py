"""OpenAI-compatible speech endpoints; credentials live only in var/secrets."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .files import Invalid

DIRECTIONS = ("input", "output")
FIELDS = [
    {"key": "url", "label": "Provider address", "type": "text", "default": "",
     "help": "OpenAI-compatible base address, ending in /v1. Resolved by the app, not your browser."},
    {"key": "model", "label": "Model", "type": "text", "default": ""},
]


def secret(direction: str) -> str:
    return f"speech-{direction}"


def validate(data: Any) -> dict:
    if not isinstance(data, dict):
        raise Invalid("speech", "Speech settings are an object.")
    out = {}
    for direction, raw in data.items():
        if direction not in DIRECTIONS or not isinstance(raw, dict):
            raise Invalid("speech", "Speech settings contain input and output objects.")
        clean = {}
        for k, v in raw.items():
            path = f"speech.{direction}.{k}"
            if k not in ("url", "model"):
                raise Invalid(path, "That is not a speech setting.")
            if not isinstance(v, str) or len(v) > 2000 or any(c in v for c in "\r\n\0"):
                raise Invalid(path, "This must be a single line of text.")
            v = v.strip()
            if k == "url" and v:
                try:
                    u = urlsplit(v)
                    _ = u.port
                except ValueError:
                    raise Invalid(path, "Use a valid provider address.") from None
                if (u.scheme not in ("http", "https") or not u.hostname or u.username
                        or u.password or u.query or u.fragment or any(c.isspace() for c in v)):
                    raise Invalid(path, "Use an http or https address without credentials, a query or a fragment.")
                v = v.rstrip("/")
            if v:
                clean[k] = v
        out[direction] = clean
    return out


async def probe_input(cfg, services) -> dict:
    return _probe(cfg, "input")


async def probe_output(cfg, services) -> dict:
    return _probe(cfg, "output")


def _probe(cfg, direction: str) -> dict:
    s = getattr(cfg, "settings", None)
    endpoint = (getattr(s, "speech", {}) or {}).get(direction, {})
    if not endpoint.get("url") or not endpoint.get("model"):
        return {"state": "unset", "detail": "Add the provider address and model."}
    # There is no universal non-generating STT health route. Configuration is not a
    # claim that the microphone, scopes, model or playback have been tested.
    return {"state": "ready", "detail": "Configured. Try dictation or playback to test it."}
