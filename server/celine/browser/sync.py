"""The calls to its browser box (browser/browserd.py, at the address in Settings, Features), with the
one key (keys/browser.key) and the time limits, the connection to its screen (Xvnc), and the
look every few seconds that tells the pages when something changed. The box answers
with facts; the skill (skills/browser.py) words them."""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

import anyio
import httpx

from ..settings import services as services_mod
from ..core.event_types import BROWSER_STATE

log = logging.getLogger(__name__)

CONNECT, READ = 5.0, 25.0     # seconds; browserd answers a look within 20
STEP_READ = 12.0              # seconds more per step: a step waits 10 s at most
RESTART_WAIT = 30.0           # seconds a restart is waited for: two launches of 10 s and more
RESTART_POLL = 0.5            # seconds between two looks while browserd starts again
VNC_PORT = 5900               # Xvnc, on the box's host
EVERY = 5                     # seconds between two looks for the pages


class BrowserRefused(Exception):
    """The box said no: its HTTP status and facts, {"error": code, ...}."""

    def __init__(self, status: int, facts: dict):
        super().__init__(f"{status} {facts.get('error')}")
        self.status, self.facts = status, facts


def url(cfg) -> str:
    return services_mod.url(cfg, "browser")


def _client(cfg, read: float = READ) -> httpx.AsyncClient:
    return services_mod.client(cfg, "browser", read, connect=CONNECT)


def _facts(r: httpx.Response) -> dict:
    try:
        data = r.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if r.status_code != 200:
        raise BrowserRefused(r.status_code, data)
    return data


async def do(cfg, tab: str, action: str, **args) -> dict:
    """POST /do: a look's facts, or BrowserRefused. httpx.HTTPError: no box."""
    return await post(cfg, {"tab": tab, "do": action, **args})


async def post(cfg, body: dict) -> dict:
    """POST /do as given (steps, or a look): the page's facts, or BrowserRefused.
    httpx.HTTPError: no box."""
    async with _client(cfg, READ + STEP_READ * len(body.get("steps") or [])) as client:
        return _facts(await client.post(url(cfg) + "/do", json=body))


async def status(cfg) -> dict:
    async with _client(cfg) as client:
        return _facts(await client.get(url(cfg) + "/status"))


async def close_tab(cfg, tab: str) -> bool:
    """A tab closed from the app's side (browser/tabs.py): True when it is closed or there
    is no such tab; False with no box, or any other refusal (busy), which is logged."""
    try:
        await post(cfg, {"tab": tab, "steps": [{"do": "close"}]})
    except BrowserRefused as e:
        if e.facts.get("error") == "no_tab":
            return True
        log.warning("tab not closed", extra={"event": "browser.close_refused",
                                             "tab": tab, "error": e.facts.get("error")})
        return False
    except httpx.HTTPError:
        return False
    return True


async def restart(cfg, tab: str) -> dict:
    """The agent's restart, refused while the user has the browser: as _back."""
    await post(cfg, {"tab": tab, "steps": [{"do": "restart"}]})
    return await _back(cfg)


async def control(cfg, action: str, tab: str | None = None) -> dict:
    """The user's take, give or restart from the live view: {"taken": since or None};
    a restart as _back. front or close on a tab from the strip: {"front": tab or None}."""
    body = {"action": action} if tab is None else {"action": action, "tab": tab}
    async with _client(cfg) as client:
        res = _facts(await client.post(url(cfg) + "/control", json=body))
    return await _back(cfg) if action == "restart" else res


async def _back(cfg) -> dict:
    """After a restart, waits until the browser is up or down again: its status then,
    or {"state": "starting"} after RESTART_WAIT."""
    with anyio.move_on_after(RESTART_WAIT):
        while True:
            await anyio.sleep(RESTART_POLL)
            try:
                now = await status(cfg)
            except (httpx.HTTPError, BrowserRefused):
                continue                 # browserd is between two runs
            if now.get("state") in ("up", "down"):
                return now
    return {"state": "starting"}


async def screen(cfg) -> anyio.abc.SocketStream:
    """A connection to the box's screen: RFB from Xvnc. OSError: none."""
    from ..errors import not_set_up
    host = urlsplit(url(cfg)).hostname
    if not host:
        raise OSError(not_set_up("the browser"))
    return await anyio.connect_tcp(host, VNC_PORT)


async def watch(cfg, store) -> None:
    """Every EVERY seconds the box's status: the tab rows follow it (browser/tabs.py),
    and every open page hears browser.state when it changed (up or not, the take-over, a
    tab's address or busy)."""
    from . import tabs
    told = None
    while True:
        try:
            now = await status(cfg) if url(cfg) else None
        except (httpx.HTTPError, BrowserRefused):
            now = None
        if now is not None:
            try:
                await tabs.follow(cfg, store, now)
            except Exception:
                log.exception("tabs not followed", extra={"event": "browser.tabs_failed"})
        if now != told:
            told = now
            store.emit_global({"type": BROWSER_STATE})
        await anyio.sleep(EVERY)


async def probe(cfg, services) -> dict[str, str]:
    """Settings, Features: the browser box at the saved address answers /status with Brave
    up; Brave down or starting is missing with browserd's reason. No saved address: the
    default one is tried, and an answer comes back with `url`, the address the setup form
    prefills."""
    from ..errors import (bad_key, her_bad_key, her_not_set_up, her_unreachable,
                          not_set_up, unreachable_words)
    saved = url(cfg)
    at = saved or services_mod.default("browser")
    host = urlsplit(at).netloc
    found = {} if saved else {"url": at}
    try:
        async with _client(cfg, read=2.5) as client:
            now = _facts(await client.get(at + "/status"))
    except httpx.HTTPError as e:
        if not saved:
            return {"state": "unset", "detail": not_set_up("the browser"),
                    "why": her_not_set_up("the browser")}
        return {"state": "missing", "detail": unreachable_words("the browser", at, e),
                "why": her_unreachable("the browser", at)}
    except BrowserRefused as e:
        if e.status in (401, 403):
            return {"state": "missing", "detail": bad_key("the browser", at),
                    "why": her_bad_key("the browser", at), **found}
        return {"state": "missing", "detail": f"The browser box at {host} answered {e.status}.",
                **found}
    if now.get("state") != "up":
        why = now.get("reason") or f"it is {now.get('state') or 'down'}"
        return {"state": "missing", "detail": f"The browser box answers at {host} but Brave "
                                              f"is not running: {why}.", **found}
    if saved:
        return {"state": "ready", "detail": f"The browser at {host} answers."}
    return {"state": "ready", "detail": f"Found at {at}.", "url": at}
