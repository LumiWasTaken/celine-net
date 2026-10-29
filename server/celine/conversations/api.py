"""The conversation routes: the list, one conversation's snapshot, sending, the tree
edits, and continuing or stopping a turn."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .. import attachments as attachments_mod
from ..api.cursor import page_cursor, read_cursor
from ..api.deps import cfg_of, json_body, settings_of, store_of
from ..settings import conversations as conv
from ..errors import Busy, CelineError, NotFound
from ..turns import store as turns
from . import store, tree, view
from ..core.event_types import CONVERSATION_SETTINGS

log = logging.getLogger("celine.api")
router = APIRouter(prefix="/api")


@router.get("/conversations")
async def list_conversations(request: Request, limit: int = 200,
                             cursor: str | None = None, archived: bool = False,
                             trashed: bool = False, place: str | None = None) -> dict:
    """A page of conversations (store.list: running first, then pinned, then the most
    recently updated, each row with its `group` and `state`); with `archived`
    only the archived ones, with `trashed` only the deleted ones, else only the others;
    `place` "celine" only its chats. `count`: how many that shelf holds. `next` is the
    cursor of the following page, null on the last one."""
    from ..clock import zone
    limit = max(1, min(limit, 500))
    rows = await store.list(store_of(request), limit + 1, read_cursor(cursor), archived,
                            trashed, place=place, tz=zone(cfg_of(request)))
    more = len(rows) > limit
    rows = rows[:limit]
    last = rows[-1] if rows else None
    nxt = page_cursor((last["rank"], last["updated_at"], last["id"])) if more else None
    for r in rows:
        del r["rank"]
    return {"conversations": rows, "next": nxt,
            "count": await store.count(store_of(request), archived, trashed, place)}


@router.post("/conversations")
async def create_conversation(request: Request) -> dict:
    """A chat (kind `chat`): {"title"?: str, "model"?: "provider/model"|null,
    "thinking"?: "off"|"low"|"medium"|"high"|null, "tools"?: {"chat_archive": false,
    "chat_archive__search": true}}. Everything is checked before the conversation
    exists: a bad value is a 400 and nothing is created. Answers {"id", "settings"}."""
    from ..settings.files import Invalid
    body = await json_body(request) if await request.body() else {}
    title = body.pop("title", "") or ""
    if not isinstance(title, str) or len(title) > 200:
        raise Invalid("title", "The title is text of at most 200 characters.")
    settings = getattr(cfg_of(request), "settings", None)
    if body and settings is None:
        raise CelineError("unavailable", "Settings are not loaded.", 503)
    clean = conv.validate(settings, request.app.state.services.registry, body)
    db = store_of(request)
    cid = await store.create(db, title)
    if clean:
        await conv.apply(db, cid, clean)
    log.info("conversation created", extra={"event": "conversation.created",
                                            "conversation": cid})
    out: dict = {"id": cid}
    if settings is not None:
        out["settings"] = conv.view(settings, cid, await conv.get(db, cid))
    return out


class NewMessage(BaseModel):
    text: str = ""
    attachments: list[str] = []


@router.get("/conversations/{cid}")
async def conversation(request: Request, cid: str) -> dict:
    """The snapshot as every client gets it (view.snapshot, the socket's too)."""
    try:
        return await view.snapshot(store_of(request), cid)
    except KeyError:
        raise NotFound("conversation") from None


async def writable(request: Request, cid: str) -> None:
    await store.ensure_writable(store_of(request), cid)


BLOCKS = ("persona", "talk", "about", "notes", "tools", "history")


@router.get("/conversations/{cid}/context")
async def what_she_sees(request: Request, cid: str) -> dict:
    """The next request of this conversation as a turn would build it now, measured in
    tokens, without a model call (turns/context.py, mode "dry", the same estimate
    /usage uses), with its tail: {window, shares: {notes, tools} (the most each may
    take, in tokens, turns/loads.py), blocks: [{name, tokens}] (persona, talk: the line
    about where it is and how it talks, about: what the user wrote about themselves,
    notes: the notes block, tools: the skill index, the skills' fragments and the tool
    schemas, history: the rest of the request estimate (the messages sent, the summary
    and its tail), so the blocks add up to the estimate the compaction check uses),
    notes: [{path, tokens, state, why}] (the notes it
    carries or would: included, left_out or off), loaded: {notes: [{path, since}],
    skills: [{name, since, tokens, core}]} (what it carries, each with the turns since
    a turn touched it; a skill's tool schemas in tokens, `core` when it always loads,
    `label` its name), tools: how many, auto_context: the Load automatically switch
    (api/context.py)}."""
    from ..turns.prompt import turn_parts
    from ..settings.tools_meta import skill_meta
    from ..turns import context, loads, tokens
    from ..turns.prompt import notes_tail, request_text
    db, cfg = store_of(request), cfg_of(request)

    def who(c):
        row = c.execute("SELECT auto_context FROM conversations WHERE id=?", (cid,)).fetchone()
        if row is None:
            raise NotFound("conversation")
        return bool(row[0])
    auto = await db.read(who)
    backend, blocks, tools, carried = await turn_parts(request.app.state.services, cid)
    system = "\n\n".join(t for _, t in blocks)
    reg = request.app.state.services.registry
    _, tail = await request_text(db, cfg, reg, backend, cid,
                                 {"id": None, "root_message_id": None}, carried, tools,
                                 touch=False)
    built = await context.build(db, cfg, backend, cid, system=system, tools=tools,
                                mode="dry", tail=tail)
    counter, p = tokens.counter(backend), built.parts
    out = {name: {"name": name, "tokens": 0} for name in BLOCKS}
    for name, text in blocks:
        out.setdefault(name, {"name": name, "tokens": 0})["tokens"] += counter.estimate(text)
    out["tools"]["tokens"] += int(p.get("tool_schemas") or 0)
    # the notes it carries by need ride in the tail: they count as notes, not history
    carried_notes = counter.estimate(notes_tail(cfg, carried) or "")
    out["notes"]["tokens"] += carried_notes
    # history is the rest of the request estimate the compaction check uses, so the
    # blocks add up to exactly that number (the per-block estimates of the system text
    # and the request's count differ by the message overhead and rounding)
    out["history"]["tokens"] = max(0, int(p.get("estimate") or 0) - sum(
        b["tokens"] for n, b in out.items() if n != "history"))
    labels = {n: skill_meta(n, getattr(reg.skills.get(n) if reg else None,
                                       "description", ""), cfg.substitute)["label"]
              for n in carried.skills}
    return {"window": built.window, "shares": carried.budgets,
            "blocks": list(out.values()),
            "notes": [{k: n[k] for k in ("path", "tokens", "state", "why")}
                      for n in carried.notes] + [
                {"path": x["name"], "tokens": x["tokens"], "state": "left_out",
                 "why": x["why"]} for x in built.left_out],
            "loaded": {"notes": [{"path": p, "since": s} for (k, p), s in
                                 sorted(carried.since.items()) if k == "note"],
                       "skills": [{"name": n, "since": carried.since[("skill", n)],
                                   "tokens": carried.costs.get(n, 0), "core": n in loads.CORE,
                                   "label": labels[n]}
                                  for n in carried.skills]},
            "tools": len(tools), "auto_context": auto}


@router.delete("/conversations/{cid}/context/skills/{name}")
async def unload_skill(request: Request, cid: str, name: str) -> dict:
    """The agent lets go of a skill it carries here (the Context tab's Unload): its tools leave
    its list until it loads it again or a message needs it. A core skill always loads:
    400."""
    from ..settings.files import Invalid
    from ..turns import loads
    if name in loads.CORE:
        raise Invalid("name", "This skill always loads.")
    if not await loads.unload(store_of(request), cid, "skill", name):
        raise NotFound("conversation")
    return {"ok": True}


@router.get("/conversations/{cid}/messages/{mid}/raw")
async def raw_response(request: Request, cid: str, mid: str) -> dict:
    """What the model's server answered for this reply (conversations/tree.py); `raw`
    null when nothing was kept (an older reply). 404: no such message."""
    try:
        return {"raw": await tree.raw_response(store_of(request), cid, mid)}
    except KeyError:
        raise NotFound("message") from None


@router.get("/conversations/{cid}/messages/{mid}/requests")
async def requests_sent(request: Request, cid: str, mid: str) -> dict:
    """Every request that streamed into this reply, as it went out, one per step
    (turns/store.requests_of), and the guard's decisions on its calls (`guard`: one
    row per call the judge looked at, "waiting" while it has not answered)."""
    from ..guard import store as guard_decisions
    store = store_of(request)
    return {"requests": await turns.requests_of(store, mid),
            "guard": await store.read(lambda c: guard_decisions.of_message(c, mid))}


class ConversationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")   # the kind is fixed at creation
    title: str | None = Field(default=None, max_length=200)
    pinned: bool | None = None
    notes_off: list[str] | None = Field(default=None, max_length=500)


def _note_paths(paths: list[str]) -> list[str]:
    """Notebook paths as given, each once: relative, no '..', at most 300 characters."""
    from ..settings.files import Invalid
    out: list[str] = []
    for p in paths:
        p = p.strip().lstrip("/")
        if not p or len(p) > 300 or ".." in p.split("/"):
            raise Invalid("notes_off", "notes_off is a list of notebook paths, such as "
                                       "lore/sea.md.")
        if p not in out:
            out.append(p)
    return out


@router.patch("/conversations/{cid}")
async def patch_conversation(request: Request, cid: str, body: ConversationPatch) -> dict:
    """The given fields only. `notes_off`: the notes left out here (the whole list); a
    change is a `conversation.notes_off` event. The kind is fixed at creation."""
    notes_off = _note_paths(body.notes_off) if body.notes_off is not None else None
    ok = await store.update(store_of(request), cid, body.title, body.pinned,
                            notes_off=notes_off)
    if not ok:
        raise NotFound("conversation")
    return {"ok": True}


@router.delete("/conversations/{cid}")
async def delete_conversation(request: Request, cid: str, for_good: bool = False) -> dict:
    """Any kind: trashed with its sub-agents, nothing removed (restore brings it back).
    `for_good`: one in the Trash removed for good, with everything that is part of it
    (store.purge); 409 when it is not in the Trash. Refused while it works."""
    try:
        if for_good:
            ok = await store.purge(store_of(request), cfg_of(request), cid) is not None
        else:
            ok = await store.delete(store_of(request), cid)
    except tree.Busy:
        raise Busy("Stop the running turn first.") from None
    except ValueError as e:
        raise CelineError("not_trashed", str(e), 409) from None
    if not ok:
        raise NotFound("conversation")
    return {"ok": True}


@router.post("/conversations/{cid}/restore")
async def restore_conversation(request: Request, cid: str) -> dict:
    if not await store.restore(store_of(request), cid):
        raise NotFound("deleted conversation")
    return {"ok": True}


@router.post("/conversations/{cid}/archive")
async def archive_conversation(request: Request, cid: str) -> dict:
    """The conversation and its sub-agents leave the list, every message kept. A
    message to it brings it back. Refused while any of them works. With Lock archived
    chats refused only while a turn of the user's is live: its turns and jobs there are
    stopped."""
    try:
        out = await store.archive(store_of(request), cid, True)
    except tree.Busy:
        raise Busy("Stop the running turn first.") from None
    if out is None:
        raise NotFound("conversation")
    dispatcher = request.app.state.services.dispatcher
    if dispatcher is not None and await store_of(request).read(store.lock_archived):
        # its work there stops
        await dispatcher.abort_conversation(
            await store_of(request).read(lambda c: store.top_of(c, cid)))
    return out


@router.post("/conversations/{cid}/unarchive")
async def unarchive_conversation(request: Request, cid: str) -> dict:
    out = await store.archive(store_of(request), cid, False)
    if out is None:
        raise NotFound("conversation")
    return out


@router.get("/messages")
async def search(request: Request, q: str = "", limit: int = 30, place: str | None = None,
                 archived: bool = False, trashed: bool = False) -> dict:
    """Found in messages, in what GET /conversations lists:
    `hits`, each with the message, its `snippet`, the `marks` of the matched words in
    it and the `link` of its conversation (homes.of), best first. `titles`: the ids of
    the conversations on that shelf whose title matches (store.titled), in rank order;
    `place` "celine": its chats only."""
    q = q.strip()
    if not q:
        return {"hits": [], "titles": []}
    limit = max(1, min(limit, 100))
    titles = await store_of(request).read(
        lambda c: store.titled(c, q, limit, archived, trashed, place))
    if archived or trashed:
        return {"hits": [], "titles": titles}
    kinds = ("chat",) if place == "celine" else ("chat", "mission", "agent")
    hits = await store.search(store_of(request), q, None, limit, 24, kinds)
    from .. import homes

    def links(c):
        for h in hits:
            h["link"] = homes.of(c, h["conversation_id"])["link"]
    await store_of(request).read(links)
    return {"hits": hits, "titles": titles}


@router.post("/conversations/{cid}/messages")
async def post_message(request: Request, cid: str, body: NewMessage) -> dict:
    """Sends the user's message to the conversation (an intercom message from `user`).
    Idle, it lands at once with a queued turn; while a turn runs it reaches it at its
    next step, and message_id, turn_id and seq are null."""
    await writable(request, cid)
    cfg = cfg_of(request)
    if len(body.text.encode()) > cfg.r("max_message_bytes"):
        from ..errors import TooLarge
        raise TooLarge("This message", len(body.text.encode()),
                       cfg.r("max_message_bytes"))
    if not body.text.strip() and not body.attachments:
        raise CelineError("bad_request", "Nothing to send.", 400)
    # a slash command is run, never sent to the model as text (commands/)
    from .. import commands
    if hit := commands.parse(body.text):
        raise CelineError("is_command", f"/{hit[0]} is a command: run it with POST "
                          f"/api/conversations/{cid}/slash-commands/{hit[0]}/run. Start the message "
                          f"with // to send it as text.", 400,
                          {"command": hit[0], "text": hit[1]})
    body.text = commands.literal(body.text)
    if len(body.attachments) > cfg.r("max_attachments"):
        raise CelineError("bad_request",
                          f"At most {cfg.r('max_attachments')} attachments per message.",
                          400)
    if body.attachments:
        rows = [await attachments_mod.get(store_of(request), a) for a in body.attachments]
        if any(r is None for r in rows):
            raise NotFound("attachment")
        if any(r["kind"] == "image" for r in rows):
            sup = await _chat_model(request, cid, None)
            if sup["blocked"]:
                raise CelineError("cannot_see_images",
                                  f"{sup['model']} can't see images. Remove the images "
                                  f"to send the message.", 400, sup)
    try:
        return await request.app.state.services.intercom.send(
            "user", f"agent:{cid}", body.text, attachments=body.attachments)
    except KeyError:
        raise NotFound("conversation") from None
    except ValueError as e:
        raise CelineError("bad_request", str(e), 400) from None


async def _chat_model(request: Request, cid: str | None, model: str | None) -> dict:
    """What the chat model that answers this conversation (or, before a new
    conversation exists, `model` or the Chat role's first) can do: `tools`, whether it
    takes a tool list at all, and attachments.support for images, plus `blocked`: a
    message with images cannot be sent, because the model can't see images and no
    Photo descriptions model answers. No chat model yet: never blocked (the message
    waits for one)."""
    cfg = cfg_of(request)
    llm = request.app.state.services.llm
    factory = request.app.state.services.dispatcher.backend_factory
    if factory is not None:                      # a test backend
        fixed = factory()
        sup = await attachments_mod.support(cfg, llm, None, fixed=fixed)
        tools = bool(fixed.supports_tools)
    else:
        s = getattr(cfg, "settings", None)
        chain = s.chat_chain(cid) if s is not None else []
        ref = model or (chain[0] if chain else None)
        if ref is None:
            return {"model": "", "tools": True, "sees": False, "describer": None,
                    "blocked": False}
        sup = await attachments_mod.support(cfg, llm, ref)
        tools = "tools" in ((s.model(ref) or {}).get("caps") or [])
    return {**sup, "tools": tools, "blocked": not sup["sees"] and sup["describer"] is None}


@router.get("/chat-model")
async def chat_model(request: Request, conversation: str | None = None,
                     model: str | None = None) -> dict:
    """What the chat model can do now: {model, tools, sees, describer, blocked}. The
    composer asks when the conversation or its model changes: it says in one line
    when the model can't use tools, and shows a banner and keeps Send off when a
    message has images and `blocked`."""
    s = getattr(cfg_of(request), "settings", None)
    if model is not None and (s is None or s.model(model) is None):
        raise NotFound("model")
    return await _chat_model(request, conversation, model)


@router.post("/conversations/{cid}/regenerate")
async def regenerate(request: Request, cid: str) -> dict:
    """A reply for the last message when it is yours and nothing answered it."""
    await writable(request, cid)
    try:
        tid, seq = await tree.regenerate(store_of(request), cid)
    except KeyError:
        raise NotFound("conversation") from None
    except ValueError as e:
        raise CelineError("bad_request", str(e), 400) from None
    request.app.state.services.dispatcher.notify()
    return {"turn_id": tid, "seq": seq}


class ContinueBody(BaseModel):
    wait: bool = False


@router.post("/conversations/{cid}/turns/{tid}/continue")
async def continue_turn(request: Request, cid: str, tid: str,
                        body: ContinueBody | None = None) -> dict:
    """Pick an interrupted, stopped, capped, retryably failed or cut-off turn up again,
    as the same turn: its steps are kept, read-only steps that did not finish run
    again, a cut-off reply is continued in the same message. `wait` (Keep waiting on
    a turn that failed `busy`): its model is waited for until Stop."""
    await writable(request, cid)
    registry = request.app.state.services.registry
    try:
        tid, seq = await turns.continue_turn(store_of(request), cid, tid, registry,
                                             wait=bool(body and body.wait))
    except KeyError:
        raise NotFound("turn") from None
    except ValueError as e:
        raise CelineError("not_continuable", str(e), 400,
                          {"reason": str(e)}) from None
    request.app.state.services.dispatcher.notify()
    return {"turn_id": tid, "seq": seq}


@router.post("/conversations/{cid}/stop")
async def abort(request: Request, cid: str) -> dict:
    """Idempotent and instant. Does not wait for the tool to actually die."""
    return {"turn_id": await request.app.state.services.dispatcher.abort_conversation(cid)}


class Target(BaseModel):
    message_id: str


class EditBody(BaseModel):
    text: str = Field(min_length=1)


@router.post("/conversations/{cid}/retry")
async def retry(request: Request, cid: str, body: Target) -> dict:
    await writable(request, cid)
    try:
        tid, seq = await tree.retry(store_of(request), cid, body.message_id)
    except KeyError:
        raise NotFound("message") from None
    except ValueError as e:
        raise CelineError("bad_request", str(e)) from None
    request.app.state.services.dispatcher.notify()
    return {"turn_id": tid, "seq": seq}


@router.post("/conversations/{cid}/messages/{mid}/versions")
async def edit(request: Request, cid: str, mid: str, body: EditBody) -> dict:
    """A new version of a message with the new text, the active leaf from then on;
    what followed the old one stays on its path. Yours is answered (turn_id); its
    reply is not (turn_id null): its tool calls stay, only its text is new."""
    await writable(request, cid)
    try:
        mid, tid, seq = await tree.edit(store_of(request), cid, mid, body.text)
    except KeyError:
        raise NotFound("message") from None
    except ValueError as e:
        raise CelineError("bad_request", str(e)) from None
    if tid:
        request.app.state.services.dispatcher.notify()
    return {"message_id": mid, "turn_id": tid, "seq": seq}


@router.post("/conversations/{cid}/messages/{mid}/continue")
async def continue_message(request: Request, cid: str, mid: str) -> dict:
    """The agent's finished reply at the active leaf, carried on as a new version of it: a turn
    sends the conversation with its text as a prefilled assistant turn and what comes
    is appended. -> {message_id, turn_id, seq}, or {refused: the sentence why}."""
    await writable(request, cid)
    try:
        new, tid, seq = await tree.continue_message(store_of(request), cid, mid)
    except KeyError:
        raise NotFound("message") from None
    except ValueError as e:
        return {"refused": cfg_of(request).substitute(str(e))}
    request.app.state.services.dispatcher.notify()
    return {"message_id": new, "turn_id": tid, "seq": seq}


@router.put("/conversations/{cid}/messages/{mid}/voice-sample")
async def love_this(request: Request, cid: str, mid: str) -> dict:
    """"Love this": its reply as a voice sample in samples.md (conversations/
    samples.py), with the date and the model that wrote it; the note's earlier text is
    kept as a version. -> {sample: true}."""
    return {"sample": await _sample(request, cid, mid, True)}


@router.delete("/conversations/{cid}/messages/{mid}/voice-sample")
async def unlove_this(request: Request, cid: str, mid: str) -> dict:
    """The sample out of the note again. -> {sample: false}."""
    return {"sample": await _sample(request, cid, mid, False)}


async def _sample(request: Request, cid: str, mid: str, on: bool) -> bool:
    from . import samples
    await writable(request, cid)
    return await samples.mark(store_of(request), cfg_of(request), cid, mid, on)


class ImpersonateBody(BaseModel):
    start: str = Field(default="", max_length=2000)


@router.post("/conversations/{cid}/impersonate")
async def impersonate(request: Request, cid: str,
                      body: ImpersonateBody | None = None) -> dict:
    """The user's next message as the chat model writes it in their voice (a one-off
    request, thinking off), beginning with what they typed (`start`). Nothing is sent
    or stored: {text} goes to the composer."""
    from ..backends.base import BackendError
    from ..backends.queue import Priority
    from . import impersonate as imp
    if await store_of(request).read(lambda c: c.execute(
            "SELECT 1 FROM conversations WHERE id=?", (cid,)).fetchone()) is None:
        raise NotFound("conversation")
    await writable(request, cid)
    factory = request.app.state.services.dispatcher.backend_factory
    fixed = factory() if factory is not None else None
    llm = request.app.state.services.llm
    backend = llm.bind(fixed, role="chat", priority=Priority.INTERACTIVE,
                       conversation_id=cid, thinking="off", voice=False,
                       fallback=False) if llm is not None else fixed
    if backend is None:
        raise CelineError("unavailable", "No chat model is set up.", 503)
    try:
        text = await imp.write(store_of(request), cfg_of(request), backend, cid,
                               (body or ImpersonateBody()).start)
    except BackendError as e:
        raise CelineError("failed", str(e), 502) from None
    return {"text": text}


@router.put("/conversations/{cid}/active-version")
async def version(request: Request, cid: str, body: Target) -> dict:
    """Moves a pointer. Never runs a turn."""
    try:
        return {"seq": await tree.switch_version(store_of(request), cid, body.message_id)}
    except KeyError:
        raise NotFound("message") from None


@router.get("/conversations/{cid}/messages/{mid}/siblings")
async def siblings(request: Request, cid: str, mid: str) -> dict:
    try:
        return await tree.siblings(store_of(request), cid, mid)
    except KeyError:
        raise NotFound("message") from None


@router.delete("/conversations/{cid}/messages/{mid}")
async def delete_message(request: Request, cid: str, mid: str) -> dict:
    """The message removed for good with everything after it in its branch
    (store.delete_message_sync). -> {message_ids, active_leaf_id}."""
    db = store_of(request)
    try:
        return await db.write(lambda c: store.delete_message_sync(c, db, cid, mid))
    except KeyError:
        raise NotFound("message") from None


# ---- one conversation's settings: model, thinking, tool switches -------------------------

@router.get("/conversations/{cid}/settings")
async def get_conversation_settings(request: Request, cid: str) -> dict:
    stored = await conv.get(store_of(request), cid)
    if stored is None:
        raise NotFound("conversation")
    return conv.view(settings_of(request), cid, stored)


@router.patch("/conversations/{cid}/settings")
async def patch_conversation_settings(request: Request, cid: str) -> dict:
    """JSON merge-patch: {"model"?: "provider/model"|null, "thinking"?: "off"|"low"|
    "medium"|"high"|null, "tools"?: {"chat_archive": false, "chat_archive__search": true,
    "shell": null}}. A key left out stays; null goes back to the default. Idempotent."""
    body = await json_body(request)
    clean = conv.validate(settings_of(request), request.app.state.services.registry, body)
    db = store_of(request)
    before = settings_of(request).chat_chain(cid)[:1]
    if not await conv.apply(db, cid, clean):
        raise NotFound("conversation")
    out = conv.view(settings_of(request), cid, await conv.get(db, cid))
    await db.append_event(cid, CONVERSATION_SETTINGS, out)
    await attachments_mod.after_switch(db, cfg_of(request),
                                       request.app.state.services.llm, cid,
                                       before[0] if before else None)
    return out


class BranchBody(BaseModel):
    message_id: str


@router.post("/conversations/{cid}/branch")
async def branch(request: Request, cid: str, body: BranchBody) -> dict:
    """Branch from here (tree.branch): a new chat with every message up to and including
    `message_id`, named by the server; event conversation.created. -> {id, title}."""
    out = await tree.branch(store_of(request), cid, body.message_id)
    if out is None:
        raise NotFound("message")
    return out


@router.get("/conversations/{cid}/export")
async def export(request: Request, cid: str) -> Response:
    """Export: the conversation's path as a Markdown file (title, then each message under
    who wrote it; tool rows left out)."""
    try:
        msgs = await store_of(request).read(lambda c: tree.path(c, cid))
    except KeyError:
        raise NotFound("conversation") from None
    title = await store.title(store_of(request), cid) or "Untitled"
    her, you = ("{{name}}\n{{username}}").split("\n", 1)
    lines = [f"# {title}", ""]
    for m in msgs:
        if m["role"] in ("user", "assistant"):
            lines += [f"## {you if m['role'] == 'user' else her}", "", m["content"] or "", ""]
    name = "".join(ch if ch.isalnum() else "-" for ch in title)[:60].strip("-") or "conversation"
    return Response("\n".join(lines), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}.md"'})
