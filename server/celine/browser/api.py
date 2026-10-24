"""The agent's browser for the page /browser: its state and tabs, the user's controls (take
over, give back, restart), the live view, the box's screen as RFB bytes relayed both
ways between the page's noVNC and Xvnc, and the console, its tools run for the user."""
from __future__ import annotations

import json
import logging

import anyio
import httpx
from fastapi import APIRouter, Request, WebSocket
from pydantic import BaseModel

from ..api.origin import ws_origin_ok
from ..api.deps import GONE, page_frames
from .. import homes
from ..errors import CelineError, not_set_up, unreachable_words
from ..skills import ToolContext
from . import sync, tabs
from .sync import BrowserRefused
from ..core.event_types import BROWSER_STATE

log = logging.getLogger("celine.browser")

router = APIRouter(prefix="/api")
ws_router = APIRouter()      # the socket lives under /ws/, not /api/


NO_BOX = not_set_up("the browser")


@router.get("/browser")
async def browser(request: Request) -> dict:
    """The box's status: state (starting, up or down; unreachable when it does not
    answer, off without a box), reason, taken (since when the user has it, epoch, or
    null), front (the tab on the screen, or null), screen ({w, h}: the live view's size
    in px, the box's) and its tabs with name, url, title, busy and conversation (the
    one that opened it, {id, title, kind, link}, or null: the user's own, or too new to
    know)."""
    cfg = request.app.state.services.cfg
    if not sync.url(cfg):
        return {"state": "off", "reason": NO_BOX, "taken": None, "tabs": []}
    try:
        out = await sync.status(cfg)
    except httpx.HTTPError as e:
        return {"state": "unreachable", "reason": unreachable_words("the browser", sync.url(cfg), e),
                "taken": None, "tabs": []}
    except BrowserRefused as e:
        raise _refused(e) from None
    by = await tabs.openers(request.app.state.services.store)
    out["tabs"] = [{**t, "conversation": by.get(t.get("name"))} for t in out.get("tabs") or []]
    return out


class Control(BaseModel):
    action: str
    tab: str | None = None


@router.post("/browser/control")
async def control(request: Request, body: Control) -> dict:
    """Take over (its calls are refused, Xvnc takes your keys and mouse), give back (its
    next result says when you had it), or restart (answers once it is up or down).
    front and close (with tab): that tab to the front or closed, {"front"}; a tab in a
    call is not closed, nor another one brought over it: {"refused": the sentence}, 200."""
    if body.action not in ("take", "give", "restart", "front", "close"):
        raise CelineError("bad_request", "action is take, give, restart, front or close.", 400)
    on_tab = body.action in ("front", "close")
    if on_tab and not body.tab:
        raise CelineError("bad_request", f"{body.action} needs a tab.", 400)
    cfg = request.app.state.services.cfg
    try:
        res = await sync.control(cfg, body.action, body.tab if on_tab else None)
    except httpx.HTTPError as e:
        raise CelineError("browser_down", unreachable_words("the browser", sync.url(cfg), e),
                          502) from None
    except BrowserRefused as e:
        code = e.facts.get("error")
        if on_tab and code == "busy":
            return {"refused": f"Tab {e.facts.get('tab') or body.tab} is in the middle of "
                               "a call. Try again when it is done."}
        if on_tab and code == "no_tab":
            return {"refused": f"Tab {body.tab} is closed already."}
        raise _refused(e) from None
    if on_tab:
        request.app.state.services.store.emit_global({"type": BROWSER_STATE})
    return res


TOOLS = ("open", "act", "scroll", "close", "look")


class Call(BaseModel):
    tool: str
    args: dict = {}


@router.post("/browser/calls")
async def call(request: Request, body: Call) -> dict:
    """The user's console: one of its browser tools (open, act, scroll, close, look) with
    its arguments, run by the same code as its call, on the tab named in args (the tab
    in front when none is). -> {"content": the text it would read, "is_error",
    "display": as its call's row has it, its shots kept as attachments without a
    message, which the next cleanup removes}."""
    skill = request.app.state.services.registry.skills.get("browser")
    if skill is None:
        raise CelineError("unavailable", "The browser skill is off.", 409)
    if body.tool not in TOOLS:
        raise CelineError("bad_request", f"tool is one of {', '.join(TOOLS)}.", 400)
    args = dict(body.args)
    if not args.get("tab"):
        try:
            args["tab"] = (await sync.status(request.app.state.services.cfg)).get(
                "front") or "console"
        except (httpx.HTTPError, BrowserRefused):
            args["tab"] = "console"

    async def emit(*_a, **_k) -> None:
        pass

    ctx = ToolContext(conversation_id="", turn_id="", tool_call_id="",
                      store=request.app.state.services.store, config=request.app.state.services.cfg,
                      emit=emit)
    try:
        res = await getattr(skill, body.tool)(ctx, **args)
    except TypeError as e:
        raise CelineError("bad_request", f"args: {e}", 400) from None
    return {"content": res.content, "is_error": res.is_error, "display": res.display}


STATE = {"pending": "running", "running": "running", "detached": "running", "ok": "done",
         "error": "failed", "gated": "failed", "cancelled": "cancelled", "interrupted": "cancelled"}


def _loads(text: str | None) -> dict:
    try:
        out = json.loads(text or "{}")
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def call_line(name: str, args: dict) -> str:
    """The agent's call as a line: `open example.org`, `look screen: Where is Play?`, `click e14,
    type e2: python` (as the Browser page lists it)."""
    first = lambda v, n: str(v or "").strip().split("\n")[0][:n]  # noqa: E731
    tool = name.removeprefix("browser__")
    if tool == "open":
        url = str(args.get("url") or "")
        return f"open {url.removeprefix('https://').removeprefix('http://')}"
    if tool == "look":
        text = first(args.get("question") or args.get("find"), 400)
        return f"look {args.get('what') or 'view'}{f': {text}' if text else ''}"
    if tool == "act":
        words = []
        for step in args.get("steps") or []:
            if not isinstance(step, dict) or not step.get("do"):
                continue
            word = str(step.get("do") or "")
            if step.get("target"):
                word += f" {step['target']}"
            if text := first(step.get("text"), 120):
                word += f": {text}"
            words.append(word)
        return ", ".join(words) or "act"
    return tool


@router.get("/browser/calls")
async def history(request: Request, q: str = "", failed: bool = False,
                  before: str | None = None, limit: int = 100) -> dict:
    """The agent's browser calls, newest first: {calls: [{id, tool, tab, line, state (running,
    done, failed, cancelled), at, shots, display, conversation: {id, title, link}}], total,
    failed (how many failed), next (the `before` of the next page)}. `q` looks in the
    line, `failed` keeps the failed ones."""
    def fn(c) -> tuple[list, dict]:
        rows = c.execute(
            "SELECT t.id, t.name, t.arguments, t.display, t.state, "
            "coalesce(t.started_at, m.created_at) AS at, m.conversation_id, v.title "
            "FROM tool_calls t JOIN messages m ON m.id = t.message_id "
            "JOIN conversations v ON v.id = m.conversation_id "
            "WHERE t.name LIKE 'browser\\_\\_%' ESCAPE '\\' ORDER BY at DESC, t.id DESC").fetchall()
        # where each of their conversations opens, the server's
        return rows, {cid: homes.of(c, cid)["link"] for cid in {r["conversation_id"] for r in rows}}

    # note: every call is read and matched here; a paged query once it has many thousands
    rows, links = await request.app.state.services.store.read(fn)
    calls = []
    for r in rows:
        args, display = _loads(r["arguments"]), _loads(r["display"])
        state = "failed" if display.get("refused") else STATE.get(r["state"], "done")
        calls.append({"id": r["id"], "tool": r["name"].removeprefix("browser__"),
                      "tab": display.get("tab") or args.get("tab"),
                      "line": call_line(r["name"], args), "state": state, "at": r["at"],
                      "shots": len(display.get("steps") or []), "display": display,
                      "conversation": {"id": r["conversation_id"], "title": r["title"],
                                       "link": links[r["conversation_id"]]}})
    shown = [x for x in calls if q.lower() in x["line"].lower() and (not failed or x["state"] == "failed")]
    rest = [x for x in shown if before is None or x["at"] < before]
    page = rest[:max(1, min(limit, 500))]
    return {"calls": page, "total": len(calls), "failed": sum(x["state"] == "failed" for x in calls),
            "next": page[-1]["at"] if len(rest) > len(page) else None}


def _refused(e: BrowserRefused) -> CelineError:
    return CelineError("refused", f"The browser box answered {e.status}: "
                                  f"{e.facts.get('error') or 'no reason'}.", 502)


@ws_router.websocket("/ws/browser")
async def screen(ws: WebSocket):
    """Xvnc's bytes to the page as binary frames, the page's binary frames to Xvnc, as
    they come; each side waits for the other, so a slow page slows Xvnc down. Text frames
    are pings, answered with pong. Another site's page: 1008; no Xvnc or it went away:
    1011, and the page connects again."""
    if not ws_origin_ok(ws.headers, ws.app.state.allowed_origins):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        vnc = await sync.screen(ws.app.state.services.cfg)
    except OSError:
        await ws.close(code=1011)
        return
    page_left = False
    async with vnc, anyio.create_task_group() as tg:
        tg.start_soon(_to_page, ws, vnc, tg.cancel_scope)
        page_left = await _from_page(ws, vnc)
        tg.cancel_scope.cancel()
    if not page_left:
        try:
            await ws.close(code=1011)
        except RuntimeError:
            pass                             # the page is gone already


async def _to_page(ws: WebSocket, vnc, scope) -> None:
    try:
        async for chunk in vnc:
            await ws.send_bytes(chunk)
    except GONE:                             # Xvnc or the page went away
        log.debug("vnc to page ended", exc_info=True, extra={"event": "browser.vnc_ended"})
    scope.cancel()


async def _from_page(ws: WebSocket, vnc) -> bool:
    """Until one side leaves: the page's bytes to Xvnc, pongs for pings. True when the
    page left."""
    async def to_vnc(data: bytes) -> bool | None:
        try:
            await vnc.send(data)
        except (OSError, anyio.BrokenResourceError, anyio.ClosedResourceError):
            return False
        return None
    return await page_frames(ws, to_vnc)
