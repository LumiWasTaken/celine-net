"""The app's one client for its shellbox (shellbox/shellboxd.py): its address as saved now
(settings/services.py, read on every call, so a new address in Settings counts at once),
the app's key (keys.py) as the bearer token, and what an answer that is not 200 or 404
means: ToolRefused with one sentence. Every caller (skills/shell.py, skills/code.py,
terminals/, box_files.py, sub_agents/cli.py) goes through here."""
from __future__ import annotations

import re

import httpx

from .errors import ToolRefused, bad_key, her_bad_key, her_not_set_up
from .settings import services

CONNECT = 10.0          # seconds to reach the box; how long an answer may take is per call
BOX = "the shellbox"


def url(cfg) -> str:
    return services.url(cfg, "shellbox")


def client(cfg, read: float | None = 60.0,
           transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """`read`: seconds an answer may take, None for a stream that lasts."""
    return services.client(cfg, "shellbox", read, connect=CONNECT, transport=transport)


def refusal(cfg, r: httpx.Response, user: str = "") -> ToolRefused:
    """The box said no (a status other than 200 and 404): its sentence. A wrong key: with
    `user` (a tool's call, she reads it) the fact and who to tell, else (a page) where
    the user fixes it."""
    try:
        said = str(r.json().get("error") or "").strip()
    except (ValueError, AttributeError):
        said = ""
    if r.status_code in (401, 403):
        return ToolRefused(f"{her_bad_key(BOX, url(cfg))} Tell {user}." if user
                           else bad_key(BOX, url(cfg)))
    if r.status_code in (400, 409, 502, 503):     # 502: a program behind the box said no
        return ToolRefused(said or "The shellbox refused this call.")
    return ToolRefused(f"The shellbox answered {r.status_code}{': ' + said if said else '.'}")


async def call(cfg, method: str, path: str, body: dict | None = None, *, read: float = 60.0,
               transport: httpx.AsyncBaseTransport | None = None,
               user: str = "") -> tuple[int, dict]:
    """One JSON call: 200 and 404 come back with their answer. ToolRefused when no
    address is saved or the box said no (refusal); httpx.HTTPError when it did not
    answer."""
    if not url(cfg):
        raise ToolRefused(her_not_set_up(BOX))
    async with client(cfg, read, transport) as c:
        r = await c.request(method, url(cfg) + path, json=body)
    if r.status_code not in (200, 404):
        raise refusal(cfg, r, user)
    try:
        data = r.json()
    except ValueError:
        raise httpx.ReadError("not JSON") from None
    return r.status_code, data if isinstance(data, dict) else {}


def home_short(path: str) -> str:
    """A path in its home as it writes it: /home/celine/x -> ~/x."""
    return re.sub(r"^/home/[^/]+", "~", path)
