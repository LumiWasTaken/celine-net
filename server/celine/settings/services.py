"""The services the app reaches over the network, as Settings (var/settings/services.toml,
Settings, Features: the setup forms of shellbox, browser and web search): one `url` per
service. The boxes' keys are the stack's own (<data>/keys, celine/keys.py): the form
shows each box's key hash (`keys.key_hash`), for a box that runs elsewhere.

The environment (CELINE_SHELL_URL, CELINE_BROWSER_URL, CELINE_SEARXNG_URL: compose sets
them) is read by `default`: the urls while the section is unsaved (`seed`), and the
address a probe tries while no url is saved (else the dev compose's loopback port), so a
box started by hand gets its card in Setup with that address prefilled. Everything that
calls a box asks `url(cfg, id)` here, which answers from `cfg.settings.services`, so a
save in Settings reaches every caller at once.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from .. import keys
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
    """The saved address of a service, "" when none; what every caller uses."""
    return str((cfg.settings.services.get(sid) or {}).get("url") or "").rstrip("/")


CONNECT = 10.0              # seconds to reach a box


def client(cfg, sid: str, read: float | None, *, connect: float = CONNECT,
           transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """An HTTP client for a box: its key (<data>/keys) as the bearer, the time limits."""
    key = keys.read(cfg.data_dir, KEYS[sid])
    return httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(connect, read=read),
                             headers={"Authorization": f"Bearer {key}"})


def from_env(sid: str) -> str:
    return os.environ.get(ENV[sid], "").strip().rstrip("/")


def default(sid: str) -> str:
    """The address a probe tries while none is saved: the compose's variable, else the dev
    compose's loopback port."""
    return from_env(sid) or DEFAULTS[sid]


def value(services: dict[str, Any], sid: str, key: str) -> Any:
    default = next(f["default"] for f in FIELDS[sid] if f["key"] == key)
    return (services.get(sid) or {}).get(key, default)


def validate(data: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(data, dict):
        raise Invalid("services", "The services are an object.")
    out: dict[str, dict[str, Any]] = {}
    for sid, block in data.items():
        if sid not in IDS:
            raise Invalid(f"services.{sid}", f"'{sid}' is not a service.")
        clean = form.validate(f"services.{sid}", FIELDS[sid], block)
        if "url" in clean:
            clean["url"] = clean["url"].rstrip("/")
            if not valid_url(clean["url"]):
                raise Invalid(f"services.{sid}.url",
                              "This must be an address, such as http://shellbox:8770.")
        out[sid] = clean
    return out


def seed() -> dict[str, dict[str, Any]]:
    """The section while services.toml is unsaved: the environment."""
    return {sid: {"url": v} if (v := from_env(sid)) else {} for sid in IDS}
