"""Terminals over HTTP, and one socket per terminal page (docs/ARCHITECTURE.md,
terminals): the list, one terminal and its history, the user's controls on its own, a new
terminal of the user's, the commands log, a message to a conversation from a terminal
page, and the live view with the user's own keys going back."""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime

import anyio
import httpx
from fastapi import APIRouter, Query, Request, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..conversations import store as conversations
from .. import commands as commands_mod
from ..api.origin import ws_origin_ok
from ..api.deps import GONE, page_frames, store_of
from ..core.ids import now
from ..errors import (CelineError, NotFound, TooLarge, ToolRefused, not_set_up,
                      unreachable_words)
from ..clock import zone
from . import store as terminals
from .sync import MISSES
from ..core.event_types import TERMINAL_STATE

log = logging.getLogger("celine.terminals")
router = APIRouter(prefix="/api")
ws_router = APIRouter()      # the socket lives under /ws/, not /api/

LABELS = {"stop": "Stop program", "pause": "Pause program", "resume": "Resume program",
          "close": "Close terminal"}     # close is no command in the log: ended_by says it
FROM = {"all": None, "conversations": "conversation", "missions": "mission",
        "sub_agents": "sub_agent", "you": "you"}
RESULTS = ("all", "failed", "in_progress")
NO_RESULT_YET = ("pending", "detached")     # a call still at work, as tool_calls keeps it
KEYS_MAX = 4096          # bytes the daemon takes in one /keys call
QUEUE = 64               # chunks waiting for a slow page before its socket is closed
EVERYTHING = 1_000_000   # rows: the log filtered here, not in the store
END = json.dumps({"type": "end"})


def _sync(request):
    return request.app.state.services.terminals


async def _daemon(sync, path: str, body: dict, read: float) -> dict:
    """A daemon call: its answer, {"refused": its sentence} (answered as 200) when it
    said no, or 502 with why when it does not answer."""
    if not sync.url:
        raise CelineError("shellbox_down", not_set_up("the shellbox"), 502)
    try:
        status, res = await sync.post(path, body, read)
    except httpx.HTTPError as e:
        raise CelineError("shellbox_down", unreachable_words("the shellbox", sync.url, e),
                          502) from None
    except ToolRefused as e:
        return {"refused": str(e)}
    if status != 200:                               # 404: no terminal of that name
        return {"refused": str(res.get("error") or "The shellbox has no such terminal.")}
    return res


async def _one(request, id: str) -> dict:
    t = await terminals.get(store_of(request), id)
    if t is None:
        raise NotFound("terminal")
    return t


async def _view(store, sync, t: dict) -> dict:
    live = sync.live.get(t["id"]) if t["ended_at"] is None else None
    return {"id": t["id"], "name": t["name"], "owner": t["owner"], "may_type": t["may_type"],
            "opened_at": t["opened_at"], "ended_at": t["ended_at"],
            "ended_by": t["ended_by"], "exit": t["exit"], "live": live,
            "conversation": await terminals.opener(store, t["id"])}


# ---- the list and one terminal ------------------------------------------------------------

@router.get("/terminals")
async def list_terminals(request: Request, mission: str | None = None,
                         trashed: bool = False) -> dict:
    """Open terminals first; trashed ones left out, or only those with `trashed`. With
    `mission`, only the terminals of that mission's conversation and its sub-agents."""
    store, sync = store_of(request), _sync(request)
    out = []
    for t in await terminals.list_terminals(store, trashed):
        row = await _view(store, sync, t)
        c = row["conversation"]
        if mission is not None and (c is None or mission not in (c["id"], c["agent_of"])):
            continue
        out.append(row)
    # the shellbox: not set up, or not answering for a few rounds
    down = (not_set_up("the shellbox") if not sync.url
            else sync.down if sync.misses >= MISSES else None)
    return {"terminals": out, "down": down}


@router.post("/terminals")
async def new_terminal(request: Request) -> dict:
    """A terminal of the user's own: the first free name of theirs, opened by the
    daemon with them as owner."""
    cfg, store, sync = request.app.state.services.cfg, store_of(request), _sync(request)
    owner = terminals.slug(cfg.user) or "you"
    held = {t["name"] for t in await terminals.list_terminals(store)
            if t["ended_at"] is None} | sync.names
    name = terminals.free_name(owner, held)
    res = await _daemon(sync, "/type", {"terminal": name, "owner": owner}, 30.0)
    if "refused" in res:
        return res
    if not res.get("id"):
        raise CelineError("shellbox", "The shellbox opened no terminal.", 502)
    await terminals.upsert(store, res["id"], name, res.get("owner") or owner, now())
    await sync.refresh()
    return {"id": res["id"], "name": name}


@router.get("/terminals/{id}")
async def terminal(request: Request, id: str) -> dict:
    return await _view(store_of(request), _sync(request), await _one(request, id))


@router.get("/terminals/{id}/log")
async def history(request: Request, id: str) -> StreamingResponse:
    """The terminal's whole stored history, raw bytes as it printed them."""
    await _one(request, id)
    return StreamingResponse(terminals.history(request.app.state.services.cfg.data_dir, id),
                             media_type="application/octet-stream")


class MayType(BaseModel):
    may_type: bool


@router.patch("/terminals/{id}")
async def may_type(request: Request, id: str, body: MayType) -> dict:
    """Whether it may type into this terminal of the user's."""
    t = await _one(request, id)
    if t["owner"] == "celine":
        raise CelineError("bad_request", f"{t['name']} is "
                          f"{request.app.state.services.cfg.name}'s terminal.", 400)
    await terminals.set_may_type(store_of(request), id, body.may_type)
    return {"ok": True}


@router.delete("/terminals")
async def trash_ended(request: Request, state: str) -> dict:
    """`?state=ended`: every ended terminal into the trash."""
    if state != "ended":
        raise CelineError("bad_request", "Only state=ended can be trashed.", 400)
    n = await terminals.trash_ended(store_of(request))
    store_of(request).emit_global({"type": TERMINAL_STATE})
    return {"trashed": n}


async def _trash(request: Request, id: str, on: bool) -> dict:
    t = await _one(request, id)
    if not await terminals.set_trashed(store_of(request), id, on):
        raise CelineError("bad_request", f"{t['name']} is still open.", 400)
    store_of(request).emit_global({"type": TERMINAL_STATE})
    return {"ok": True}


@router.post("/terminals/{id}/restore")
async def restore_terminal(request: Request, id: str) -> dict:
    return await _trash(request, id, False)


@router.delete("/terminals/{id}")
async def delete_terminal(request: Request, id: str, for_good: bool = False) -> dict:
    """An ended terminal into the trash, its history stays; for_good=1: a trashed
    terminal, its commands and its history, gone."""
    if not for_good:
        return await _trash(request, id, True)
    await _one(request, id)
    if not await terminals.purge(store_of(request), request.app.state.services.cfg.data_dir, id):
        raise CelineError("conflict", "It is not in the Trash.", 409)
    store_of(request).emit_global({"type": TERMINAL_STATE})
    return {"ok": True}


# ---- controls -------------------------------------------------------------------------------

class Control(BaseModel):
    action: str


@router.post("/terminals/{id}/control")
async def control(request: Request, id: str, body: Control) -> dict:
    """Stop (Ctrl-C), pause, resume or close. All but close are logged as a command of
    the user's with the short word; on its terminals also the sentence it reads with
    its next result."""
    cfg, store, sync = request.app.state.services.cfg, store_of(request), _sync(request)
    t = await _one(request, id)
    if body.action not in LABELS:
        raise CelineError("bad_request", "action is stop, pause, resume or close.", 400)
    if t["ended_at"]:
        return {"refused": f"{t['name']} has ended."}
    res = await _daemon(sync, "/control", {"terminal": t["name"], "action": body.action}, 15.0)
    if "refused" in res:
        return res
    at = datetime.now(zone(cfg)).strftime("%H:%M")
    word, sentence = control_words(cfg.user, body.action, t["name"], res.get("program"), at)
    note = sentence if t["owner"] == "celine" else None
    if body.action == "close":
        await terminals.end(store, id, "user", None)
    else:
        await terminals.add_command(store, id, "control", LABELS[body.action], result=word,
                                    note=note)
    await sync.refresh()
    return {"ok": True, "note": note}


def control_words(user: str, action: str, name: str, program: str | None,
                  at: str) -> tuple[str, str]:
    """The log's short word, and the sentence Celine reads."""
    if action == "stop":
        if program:
            return f"{program} stopped", f"{user} stopped {program} (Ctrl-C) at {at}."
        return "Ctrl-C sent", f"{user} pressed Ctrl-C in {name} at {at}."
    what = program or "the program"
    if action == "pause":
        return f"{what} paused", f"{user} paused {what} at {at}."
    if action == "resume":
        return f"{what} continues", f"{user} resumed {what} at {at}."
    return f"{name} ended", f"{user} closed terminal {name} at {at}."


# ---- the commands log -----------------------------------------------------------------------

@router.get("/terminal-commands")
async def commands(request: Request, terminal: str | None = None,
                   from_: str = Query("all", alias="from"), result: str = "all",
                   q: str = "", mission: str | None = None, limit: int = 100,
                   before: str | None = None) -> dict:
    """The agent's shell calls and the user's lines and controls, newest first. `total` counts
    what the filters match; `next` is the `before` of the next page."""
    if from_ not in FROM:
        raise CelineError("bad_request",
                          "from is all, conversations, missions, sub_agents or you.", 400)
    if result not in RESULTS:
        raise CelineError("bad_request", "result is all, failed or in_progress.", 400)
    store, live = store_of(request), _sync(request).live
    limit = max(1, min(limit, 500))
    kind = FROM[from_]
    if kind is None and result == "all":
        rows, total = await terminals.commands(store, terminal_id=terminal, q=q or None,
                                               mission=mission, limit=limit, before=before,
                                               live=live)
    else:
        rows, _ = await terminals.commands(store, terminal_id=terminal, q=q or None,
                                           mission=mission, limit=EVERYTHING, live=live)
        rows = [r for r in rows if (kind is None or r["from"]["kind"] == kind)
                and _matches(r, result)]
        total = len(rows)
        rows = [r for r in rows if before is None or r["at"] < before][:limit]
    return {"commands": rows, "total": total,
            "next": rows[-1]["at"] if len(rows) == limit else None}


def _matches(row: dict, result: str) -> bool:
    o = row["outcome"]
    if result == "failed":
        return bool(o) and (o["state"] == "error" or o["exit"] not in (None, 0))
    if result == "in_progress":
        return bool(o) and (o["state"] == "running" or _at_work(row))
    return True


def _at_work(row: dict) -> bool:
    return row["outcome"]["state"] in NO_RESULT_YET and row["result"] is None


def outcome_words(row: dict) -> str:
    """How a command went, in a few words: `exit 0 in 1.2 s`, `running for 4 min 12 s`,
    a control's word, nothing for a typed line."""
    o = row["outcome"]
    if o is None:
        return row["result"] or ""
    state = o["state"]
    if state == "running" or _at_work(row):
        return f"running for {dur(o['running_for'])}" if o["running_for"] is not None \
            else "running"
    if o["exit"] is not None:
        return f"exit {o['exit']} in {dur(o['seconds'])}" if o["seconds"] is not None \
            else f"exit {o['exit']}"
    return state or ""


def dur(seconds) -> str:
    """1.2 s, 48 s, 14 min 2 s, 2 h 5 min."""
    s = float(seconds or 0)
    if s < 10:
        return f"{s:.1f} s"
    s = int(round(s))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60} s"
    return f"{s // 3600} h {s % 3600 // 60} min"


# ---- a message from a terminal page ---------------------------------------------------------

@router.get("/terminals/{id}/targets")
async def targets(request: Request, id: str) -> dict:
    """Where the page's message can go, by name: the terminal's conversation first."""
    store = store_of(request)
    await _one(request, id)
    own = await terminals.last_conversation(store, id)
    tz = zone(request.app.state.services.cfg)
    return {"targets": await store.read(
        lambda c: conversations.named(c, tz, own["id"] if own else None))}


class TerminalMessage(BaseModel):
    text: str = ""
    conversation_id: str | None = None      # an id, "new", or none: the terminal's
    command_id: str | None = None           # Reply: the command quoted under the text


@router.post("/terminals/{id}/messages")
async def message(request: Request, id: str, body: TerminalMessage) -> dict:
    """The user's text into a conversation, with one trailing line that links the
    terminal, or quotes one of its commands."""
    cfg, store = request.app.state.services.cfg, store_of(request)
    t = await _one(request, id)
    size = len(body.text.encode())
    if size > cfg.r("max_message_bytes"):
        raise TooLarge("This message", size, cfg.r("max_message_bytes"))
    if hit := commands_mod.parse(body.text):
        raise CelineError("is_command", f"/{hit[0]} is a command, not a message. Start "
                          f"the message with // to send it as text.", 400,
                          {"command": hit[0], "text": hit[1]})
    text = commands_mod.literal(body.text).strip()
    if not text and not body.command_id:
        raise CelineError("bad_request", "Nothing to send.", 400)
    line = f"[[terminal:{id}]] {t['name']}"
    if body.command_id:
        row = await _command(store, id, body.command_id, _sync(request).live)
        if row is None:
            raise NotFound("command")
        said = ", ".join(p for p in (row["text"], outcome_words(row)) if p)
        line = f"> {line}: {said}" if said else f"> {line}"
    cid = body.conversation_id
    if cid is None:
        c = await terminals.last_conversation(store, id)
        cid = c["id"] if c else "new"
    if cid == "new":
        cid = await conversations.create(store, "")
    elif await conversations.title(store, cid) is None:
        raise NotFound("conversation")
    await request.app.state.services.intercom.send("user", f"agent:{cid}",
                                          f"{text}\n{line}" if text else line)
    return {"conversation_id": cid, "title": await conversations.title(store, cid)}


async def _command(store, terminal_id: str, command_id: str, live: dict) -> dict | None:
    """One row of the terminal's log, by id, page by page."""
    before = None
    while True:
        rows, _ = await terminals.commands(store, terminal_id=terminal_id, before=before,
                                           live=live)
        for r in rows:
            if r["id"] == command_id:
                return r
        if len(rows) < 100:
            return None
        before = rows[-1]["at"]


# ---- the live view --------------------------------------------------------------------------

@ws_router.websocket("/ws/terminals/{id}")
async def terminal_socket(ws: WebSocket, id: str):
    """The daemon's view of the terminal, chunk by chunk as binary frames, then the text
    frame {"type": "end"} when the terminal ends. Binary frames from the page are keys
    into the user's own terminal; a ping gets a pong."""
    if not ws_origin_ok(ws.headers, ws.app.state.allowed_origins):
        await ws.close(code=1008)
        return
    await ws.accept()
    store, sync = ws.app.state.services.store, ws.app.state.services.terminals
    t = await terminals.get(store, id)
    if t is None:
        await ws.close(code=1008)
        return
    if t["ended_at"]:
        await ws.send_text(END)
        await ws.close()
        return

    tx, rx = anyio.create_memory_object_stream[bytes](QUEUE)
    how = {"clean": False, "full": False, "closed": False}

    async def read():
        try:
            how["clean"] = await sync.view(t["name"], tx.send_nowait)
        except anyio.WouldBlock:
            how["full"] = True
        finally:
            await tx.aclose()

    async def send():
        try:
            async with rx:
                async for chunk in rx:
                    await ws.send_bytes(chunk)
            if how["clean"] and not how["full"]:
                await ws.send_text(END)
        except GONE:
            how["closed"] = True
        tg.cancel_scope.cancel()

    async with anyio.create_task_group() as tg:
        tg.start_soon(read)
        tg.start_soon(send)
        how["closed"] = await _from_page(ws, store, sync, t)
        tg.cancel_scope.cancel()
    if not how["closed"]:
        try:
            await ws.close(code=1011 if how["full"] else 1000)
        except RuntimeError:
            pass


async def _from_page(ws: WebSocket, store, sync, t: dict) -> bool:
    """Until the page leaves: keys into the user's own terminal, pongs for pings. True
    when this side closed the socket."""
    async def keys(data: bytes) -> bool | None:
        row = await terminals.get(store, t["id"])
        if row is None or row["owner"] == "celine" or row["ended_at"]:
            await ws.close(code=1008)
            return True
        await _keys(store, sync, t, data)
        return None
    return await page_frames(ws, keys)


async def _keys(store, sync, t: dict, data: bytes) -> None:
    """Raw bytes to the daemon, in what it takes at once; a line the shell got with
    Enter goes into the log as typed by the user."""
    for i in range(0, len(data), KEYS_MAX):
        piece = base64.b64encode(data[i:i + KEYS_MAX]).decode()
        try:
            status, res = await sync.post("/keys", {"terminal": t["name"], "data": piece})
        except httpx.HTTPError:
            log.warning("keys lost", extra={"event": "terminals.keys_lost",
                                            "terminal": t["name"]})
            return
        except ToolRefused:             # not the user's own terminal: nothing is typed
            return
        if status == 200 and res.get("line"):
            await terminals.add_command(store, t["id"], "line", str(res["line"]))
