"""The services the app reaches over the network, as Settings (var/settings/services.toml,
Settings, Features: the setup forms of shellbox, browser and web search): one `url` per
service, each saved on its own (PUT /api/services/{id}). The boxes' keys are the stack's
own (<data>/keys, celine/keys.py): the form shows each box's key hash (`keys.key_hash`),
for a box that runs elsewhere.

The environment (CELINE_SHELL_URL, CELINE_BROWSER_URL, CELINE_SEARXNG_URL: compose sets
them) fills every service services.toml does not name (`effective`), read on every call;
a service the file names keeps its address, an empty one too (cleared). With neither, a
probe tries the dev compose's loopback port (`default`), so a box started by hand gets its
card in Setup with that address prefilled. Everything that calls a box asks `url(cfg, id)`
here, so a save in Settings reaches every caller at once.
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

import httpx

from .. import keys
from ..errors import unreachable_words
from . import form
from .files import Invalid
from .kinds import valid_url

IDS = ("shellbox", "browser", "searxng")
# the feature's key in <data>/keys (celine/keys.py NAMES); web search has none
KEYS = {"shellbox": "shell", "browser": "browser"}
ENV = {"shellbox": "CELINE_SHELL_URL", "browser": "CELINE_BROWSER_URL",
       "searxng": "CELINE_SEARXNG_URL"}
# where the dev compose publishes each service (docker-compose.dev.yml)
DEFAULTS = {"shellbox": "http://127.0.0.1:8770", "browser": "http://127.0.0.1:8771",
            "searxng": "http://127.0.0.1:8888"}

FIELDS: dict[str, list[dict[str, Any]]] = {
    "shellbox": [{"key": "url", "label": "Address", "type": "text",
                  "help": "The shellbox's address, such as http://shellbox:8770.",
                  "default": "", "word": True}],
    "browser": [{"key": "url", "label": "Address", "type": "text",
                 "help": "The browser's address, such as http://browser:8771.",
                 "default": "", "word": True}],
    "searxng": [{"key": "url", "label": "Address", "type": "text",
                 "help": "SearXNG's address, such as http://searxng:8080.",
                 "default": DEFAULTS["searxng"], "word": True}],
}

def url(cfg, sid: str) -> str:
    """A service's address, what every caller uses; "" when it has none."""
    return effective(cfg.settings.services, sid)


def effective(services: dict[str, Any], sid: str) -> str:
    """The address services.toml holds for `sid` ("" when cleared), else the compose's
    variable while the file does not name it."""
    block = services.get(sid)
    return block["url"] if block is not None else from_env(sid)


def shown(services: dict[str, Any], sid: str) -> str:
    """The address the forms show: `effective`, else the field's default (SearXNG's)."""
    return effective(services, sid) or FIELDS[sid][0]["default"]


def view(services: dict[str, Any], sid: str) -> dict[str, Any]:
    """`sid` as the forms show it: its address (`shown`), where that comes from (`source`:
    "saved", "environment" or "default") and `default`: the address Use the default puts
    back while a saved one hides the variable or the field's default, else None."""
    behind = shown({}, sid)
    saved = sid in services
    return {"url": shown(services, sid),
            "source": "saved" if saved else "environment" if from_env(sid) else "default",
            "default": behind if saved and behind else None}


def merged(services: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """`services` with `changes` ({sid: {url}}): None forgets a saved address, so the
    compose's variable fills it again."""
    out = {sid: b for sid, b in services.items() if sid not in changes}
    out.update({sid: b for sid, b in changes.items() if b is not None})
    return out


CONNECT = 10.0              # seconds to reach a box


def client(cfg, sid: str, read: float | None, *, connect: float = CONNECT,
           transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """An HTTP client for a box: its key (<data>/keys) as the bearer, the time limits."""
    key = keys.read(cfg.data_dir, KEYS[sid])
    return httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(connect, read=read),
                             headers={"Authorization": f"Bearer {key}"})


def from_env(sid: str) -> str:
    return os.environ.get(ENV[sid], "").strip().rstrip("/")


def unreachable(what: str, at: str, e: Exception, sid: str) -> str:
    """errors.unreachable_words for service `sid`: when the compose's variable names another
    address than the saved one, that comes first."""
    env = from_env(sid)
    if at and env and env != at.rstrip("/"):
        return (f"{what[0].upper()}{what[1:]} did not answer at {urlsplit(at).netloc or at}. "
                f"{ENV[sid]} is {env}; the address saved here is used instead.")
    return unreachable_words(what, at, e)


def default(sid: str) -> str:
    """The address a probe tries while the service has none: the dev compose's loopback
    port."""
    return DEFAULTS[sid]


def validate(data: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(data, dict):
        raise Invalid("services", "The services are an object.")
    out: dict[str, dict[str, Any]] = {}
    for sid, block in data.items():
        if sid not in IDS:
            raise Invalid(f"services.{sid}", f"'{sid}' is not a service.")
        clean = form.validate(f"services.{sid}", FIELDS[sid], block)
        clean["url"] = clean.get("url", "").rstrip("/")        # "": cleared
        if clean["url"] and not valid_url(clean["url"]):
            raise Invalid(f"services.{sid}.url",
                          "This must be an address, such as http://shellbox:8770.")
        out[sid] = clean
    return out
