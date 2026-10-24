"""The commands themselves: /compact, /think (/effort), /model, /usage."""
from __future__ import annotations

import json
import logging

from ..errors import CelineError, NotFound
from ..turns import store as turns
from ..turns.prompt import turn_parts
from . import COMMANDS, Arg, command
from ..core.event_types import (
    CONTEXT_COMPACT_FAILED,
    CONTEXT_COMPACT_SKIPPED,
    CONVERSATION_SETTINGS,
)

log = logging.getLogger("celine.commands")


# ---- /compact -----------------------------------------------------------------------------
async def _compact_now(store, cfg, backend, cid, system, tools, focus) -> str:
    """"done", "skipped" (nothing to compact) or "failed"; the conversation's log
    says the same (`context.compacted`, `context.compact_skipped`, `.._failed` with the
    reason)."""
    from ..turns import compact, context
    try:
        built = await context.build(store, cfg, backend, cid, system=system, tools=tools,
                                    mode="manual", focus=focus or None)
    except compact.SummaryFailed:
        return "failed"                   # the build said why (context.compact_failed)
    except Exception as e:
        log.exception("manual compaction failed", extra={"event": "command.compact_failed"})
        await store.append_event(cid, CONTEXT_COMPACT_FAILED, {
            "trigger": "manual", "messages": 0, "through_message_id": None,
            "reason": f"{type(e).__name__}: {e}"})
        return "failed"
    if not any(a.startswith("summarised") for a in built.actions):
        await store.append_event(cid, CONTEXT_COMPACT_SKIPPED, {
            "trigger": "manual", "reason": "Nothing to compact yet."})
        return "skipped"
    return "done"


_COMPACT_SAYS = {"done": "Compacted.", "skipped": "Nothing to compact yet.",
                 "failed": "Compacting failed."}


async def _compact(services, cid: str, text: str, *, wait: bool = False) -> dict:
    """In the background (the conversation's log reports the end), or with
    `wait=true` until it is done."""
    store, cfg = services.store, services.cfg
    backend, blocks, tools, _ = await turn_parts(services, cid)
    system = "\n\n".join(t for _, t in blocks)
    focus = text[:500] or None
    tg = getattr(services.dispatcher, "_tg", None)
    if wait or tg is None:
        state = await _compact_now(store, cfg, backend, cid, system, tools, focus)
        return {"state": state, "message": _COMPACT_SAYS[state]}
    tg.start_soon(_compact_now, store, cfg, backend, cid, system, tools, focus)
    return {"state": "started", "message": "Compacting."}


command("compact", "Compact older messages. Optional: what to keep in detail.",
        args=(Arg("focus", "What to keep in detail"),),
        when="idle")(_compact)


# ---- /think, /effort ----------------------------------------------------------------------
def think_levels(services, cid: str | None) -> list[str]:
    """"off" plus the words the conversation's current chat model accepts
    (settings/thinking.levels: the model's list, its provider's, or the kind's)."""
    s = getattr(services.cfg, "settings", None)
    if s is None:
        return ["off"]
    from ..settings import thinking
    chain = s.chat_chain(cid)
    if not chain:
        return ["off"]
    pid = s.split(chain[0])[0]
    words = thinking.levels(s.providers.get(pid) or {}, s.model(chain[0]) or {})
    return ["off"] + [w for w in words if w != "off"]


async def _think(services, cid: str, text: str, *, wait: bool = False) -> dict:
    """"off", "default" (the model's own), or any effort word, passed to the provider
    as typed: the suggested words are the model's, but the value is not limited to
    them (qwen knows "xhigh" and "max", others "high")."""
    from ..settings import conversations as conv
    from ..settings import thinking
    store = services.store
    level = text.split()[0] if text else ""
    offered = think_levels(services, cid)
    if not level:
        stored = await conv.get(store, cid)
        cur = (stored or {}).get("thinking")
        return {"state": "unchanged", "thinking": cur, "levels": offered,
                "message": f"Thinking: {cur or 'the model default'}."}
    if level.lower() in ("default", "reset"):
        level = None
    elif level.lower() == "off":
        level = "off"
    elif not thinking.valid(level):
        raise CelineError("bad_args", "Thinking is off, default, or one word such as "
                          + ", ".join(offered[1:4] or ["high"]) + ".", 400,
                          {"usage": COMMANDS["think"].usage, "levels": offered})
    s = getattr(services.cfg, "settings", None)
    clean = conv.validate(s, services.registry, {"thinking": level})
    if not await conv.apply(store, cid, clean):
        raise NotFound("conversation")
    # the same event PATCH /conversations/{cid}/settings sends (settings-backend)
    view = None
    if s is not None:
        view = conv.view(s, cid, await conv.get(store, cid))
        await store.append_event(cid, CONVERSATION_SETTINGS, view)
    note = "" if level in (None, "off") or level in offered else \
        " The model's list does not have this word. The app sends it as typed."
    return {"state": "changed", "thinking": level, "levels": offered,
            "message": f"Thinking: {level or 'the model default'}, from the next "
                       f"turn.{note}", "settings": view}


command("think", "How much the model thinks before it answers, in this conversation: "
        "off, default, or an effort word the model knows.",
        args=(Arg("level", "off, default, or an effort word. The model's words are "
                  "suggested. The app passes any word through."),),
        aliases=("effort",))(_think)


# ---- /model -------------------------------------------------------------------------------
def _close(name: str, ids: list[str], n: int = 6) -> list[str]:
    import difflib
    low = {i.lower(): i for i in ids}
    hits = [low[x] for x in difflib.get_close_matches(name.lower(), list(low), n=n,
                                                        cutoff=0.5)]
    hits += [i for i in ids if name.lower() in i.lower() and i not in hits]
    return hits[:n]


async def _model(services, cid: str, text: str, *, wait: bool = False) -> dict:
    """The conversation's chat model. The name is checked against what the current
    provider (the provider of the model this conversation uses now) offers, via its own
    model list (settings-backend's discovery); "provider/model" picks from another
    provider in Settings. Only models added in Settings, Models can be picked."""
    from ..settings import conversations as conv
    from ..settings import kinds
    from ..settings.registry import provider_key
    store = services.store
    s = getattr(services.cfg, "settings", None)
    if s is None:
        raise CelineError("not_available", "No model is set up. Add one in "
                          "Settings, Models.", 400)
    chain = s.chat_chain(cid)
    current = chain[0] if chain else None
    name = text.split()[0] if text else ""
    pid = s.split(current)[0] if current else None
    if not name:
        p = s.providers.get(pid) or {}
        chat = [f"{pid}/{m}" for m, v in (p.get("models") or {}).items()
                if "chat" in (v.get("caps") or [])]
        return {"state": "unchanged", "model": current, "models": chat,
                "message": f"Model: {current or 'none'}."}
    if name.lower() in ("default", "reset"):
        clean = conv.validate(s, services.registry, {"model": None})
    else:
        if "/" in name and name.split("/", 1)[0] in s.providers:
            pid, mid = name.split("/", 1)
        else:
            mid = name
        p = s.providers.get(pid or "")
        if p is None:
            raise CelineError("not_available", "No provider to pick a model from. Add one "
                              "in Settings, Models.", 400)
        label = p.get("label", pid)
        added = dict(p.get("models") or {})
        key = provider_key(pid, s.secrets_root)
        try:
            found = [m["id"] for m in await kinds.discover(p["kind"], p["base_url"], key)]
        except Exception as e:                  # it fails with the provider's reason
            check = await kinds.check(p["kind"], p["base_url"], key, key_required=False)
            raise CelineError("unreachable", check.get("sentence") or
                              f"The app could not ask {label}: {type(e).__name__}.",
                              502) from None
        by_lower = {i.lower(): i for i in found}
        by_label = {str(v.get("label", "")).lower(): k for k, v in added.items()
                    if v.get("label")}
        hit = by_lower.get(mid.lower()) or (by_label.get(mid.lower())
                                             if by_label.get(mid.lower()) in found else None)
        if hit is None:
            close = _close(mid, found)
            raise CelineError(
                "unknown_model",
                f"{label} has no model \"{mid}\"."
                + (f" Close: {', '.join(close)}." if close else ""),
                400, {"model": mid, "provider": pid, "close": close})
        if hit not in added:
            raise CelineError(
                "not_added", f"{hit} is on {label}, but it is not added in Settings, "
                f"Models.", 400, {"model": hit, "provider": pid})
        clean = conv.validate(s, services.registry, {"model": f"{pid}/{hit}"})
    if not await conv.apply(store, cid, clean):
        raise NotFound("conversation")
    view = conv.view(s, cid, await conv.get(store, cid))
    await store.append_event(cid, CONVERSATION_SETTINGS, view)
    from .. import attachments
    await attachments.after_switch(store, services.cfg, services.llm, cid, current)
    eff = (view.get("effective") or {})
    return {"state": "changed", "model": clean["model"], "settings": view,
            "message": f"Model: {eff.get('label') or eff.get('model')}, from the next "
                       f"turn."}


command("model", "Which model answers in this conversation. Checked against the "
        "models the current provider offers.",
        args=(Arg("name", "A model of the current provider, or provider/model, or "
                  "default", required=False),))(_model)


# ---- /usage -------------------------------------------------------------------------------
PARTS = ("system", "summary", "warning", "tool_schemas", "history_verbatim",
         "history_aged", "reserve_output", "margin")


async def _usage(services, cid: str, text: str, *, wait: bool = False) -> dict:
    """Token use and what is in the context: the next request as it would be built now
    (nothing is changed, no model is called), the last request as it went out with its
    real numbers (or the next one, before any was kept), and the totals still in the log."""
    from ..turns import context, tokens
    from ..turns.prompt import last_request
    store = services.store
    backend, blocks, tools, _ = await turn_parts(services, cid)
    system = "\n\n".join(t for _, t in blocks)
    built = await context.build(store, services.cfg, backend, cid,
                                system=system, tools=tools, mode="dry")
    win, p = built.window, built.parts
    # the parts that hold something, by name: {"system": 4210, ...}
    parts = {k: n for k in PARTS if (n := int(p.get(k) or 0))}
    last = await turns.last_usage(store, cid)

    def totals(c):
        n = pt = ct = 0
        for r in c.execute("SELECT data FROM events WHERE conversation_id=? AND "
                           "type='context.usage'", (cid,)):
            d = json.loads(r["data"])
            n += 1
            pt += int(d.get("prompt_tokens") or 0)
            ct += int(d.get("completion_tokens") or 0)
        return {"requests": n, "prompt_tokens": pt, "completion_tokens": ct}
    tot = await store.read(totals)
    comp = built.compaction
    pol = context.POLICY
    est = built.estimate
    msg = f"{est:,} of {win:,} tokens ({est * 100 // max(1, win)} %)."
    if comp:
        msg += f" Compacted: {comp['covers_messages']:,} messages."
    return {"state": "unchanged", "message": msg,
            "model": getattr(backend, "model", None), "window": win,
            "window_known": tokens.known_window(backend) is not None,
            "estimate": est, "free": int(p.get("free") or 0), "parts": parts,
            "messages_sent": p.get("messages_sent"),
            "messages_on_version": p.get("messages_on_version"),
            "aged_results": built.aged,
            "summary": ({"covers_messages": comp["covers_messages"],
                         "tokens": tokens.estimate(comp.get("text") or ""),
                         "created_at": comp.get("created_at"),
                         "trigger": comp.get("trigger")} if comp else None),
            "thresholds": {"shorten_at": int(pol.age_at * win),
                           "summarise_at": int(pol.compact_at * win)},
            "next": built.actions,
            # the last request as it went out; None before one was kept
            "request": last_request(services.cfg, cid),
            "last_request": last, "totals": tot}


command("usage", "Token use, and what is in {{name}}'s context now: parts and sizes.")(
    _usage)
