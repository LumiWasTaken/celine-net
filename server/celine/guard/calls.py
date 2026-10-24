"""Bastet on one call of her turn: what the tool chokepoint (turns/tools.py) asks, and
what it does with the answer. The chokepoint asks two things and nothing else of the
guard: `frozen` before any check of its own, and `at_call` after its own checks, right
before the call runs. Everything between (the closed gates, the judges, the owner's
confirm, the freeze, the note to the owner, the passes it has not heard of) is here.

Off, at_call lets every call run and says nothing. Watch never makes it wait: the call
runs at once and the rules are answered in the background, writing only their rows. On,
a call a rule stops does not run, and its wait for the judge counts on its clock."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import anyio

from ..skills import ToolResult
from . import bastet, facts, freeze as freezes, passes, rules
from . import store as rows
from .flow import check, closed, on_call
from .small_role import Unavailable
from .switch import mode as guard_mode

log = logging.getLogger("celine.tools")

FAILED = "Not run: Bastet broke while she checked this call."
FROZEN = "Not run: you are frozen. Nothing runs until {{user}} unfreezes you."
NO_RUNNER = "no background runner"


@dataclass
class Gate:
    """What Bastet says about one call. `refusal`: (state, ToolResult), the call does
    not run; `pending`: a shutdown came while it was asked (the call stays pending and
    runs on resume); else it runs, and its result gains `lines` (advice, a pass). `told`:
    the passes those lines tell, told again (passes.retell) when the call never ran."""
    refusal: tuple[str, ToolResult] | None = None
    pending: bool = False
    lines: list[str] = field(default_factory=list)
    told: list[int] = field(default_factory=list)


def _gated(text: str, tool: str, **guard) -> tuple[str, ToolResult]:
    return "gated", ToolResult(content=text, display={
        "gated": True, "tool": tool, **({"guard": guard} if guard else {})})


async def frozen(store, cfg, tool: str) -> tuple[str, ToolResult] | None:
    """Frozen, nothing runs, whatever runs the call (the house refuses; freeze.py):
    the refusal; None while it is not frozen."""
    if await freezes.frozen(store) is None:
        return None
    return _gated(cfg.substitute(FROZEN), tool, rule="frozen")


async def at_call(turn: dict, call: dict, spec, args: dict, *, store, cfg, registry,
                  token, jobs=None, progress=None) -> Gate:
    """Bastet on `call` (its `args` parsed, `spec` its ToolSpec), in the mode of its
    conversation: the closed gates (plain code, the same in Watch and On, no pass opens
    them), then the rules (On: waited for; Watch: in the jobs nursery, or at once
    without one), then the passes it has not heard of. `progress(note)`: told when a
    judge starts."""
    cid = turn["conversation_id"]
    gm = guard_mode(cfg, cid)
    if gm == "off":
        return Gate()
    mcp = facts.mcp_of(registry, call["name"])
    gcall = {**call, "args": args, "description": spec.description,
             "read_only": spec.annotations.read_only,
             "sends": mcp.sends if mcp is not None else None}
    request = turn.get("request") or []
    if (shut := await closed(turn, gcall, request, mode=gm, store=store,
                             cfg=cfg)) is not None:
        return Gate(refusal=_gated(shut.sentence, call["name"], rule="gates"))
    lines: list[str] = []
    if gm == "on":
        out = await _ask(turn, gcall, store, cfg, registry, token, progress)
        if out is None:
            return Gate(pending=True)
        if isinstance(out, tuple):
            return Gate(refusal=out)
        lines = out
    elif jobs is not None:
        jobs.tg.start_soon(watch, turn, gcall, store, cfg, registry)
    else:
        # no jobs nursery (a bare runner in a script): the row says so, at once
        await watch(turn, gcall, store, cfg, registry, _no_runner)
    told = await passes.untold(store, cid)
    return Gate(lines=lines + _pass_lines(cfg, told), told=[p["id"] for p in told])


async def _no_runner(*_):
    raise Unavailable(NO_RUNNER)


async def watch(turn, call, store, cfg, registry, ask=None) -> None:
    """Watch: Bastet on one call that already runs, in the app's nursery at background
    priority. The hijack row says waiting until it answers, "unreachable" when it cannot
    within flow.WATCH_LIMIT; nothing here reaches its turn, and nothing is raised into
    the nursery."""
    try:
        if ask is not None:
            await check(turn, call, turn.get("request") or [], mode="watch",
                        judge=ask, ask=ask, store=store, cfg=cfg)
        else:
            await on_call(turn, call, turn.get("request") or [], mode="watch",
                          app=getattr(registry, "app", None), store=store, cfg=cfg,
                          registry=registry)
    except Exception:
        log.warning("guard watch failed", exc_info=True,
                    extra={"event": "guard.watch_failed", "tool": call["name"]})


async def _ask(turn, call, store, cfg, registry, token, progress):
    """On: -> the lines its result gains (the call runs), the refusal, or None on a
    shutdown while Bastet was asked. A Stop ends the wait, for the judge and for the
    owner's answer to a confirm."""
    from ..turns.cancel import until_stop
    app = getattr(registry, "app", None)

    async def ask():
        verdict = await on_call(turn, call, turn.get("request") or [], mode="on", app=app,
                                store=store, cfg=cfg, registry=registry,
                                progress=progress)
        return await gated(verdict, turn, call, store, cfg, app)
    try:
        stopped, out = await until_stop(ask, token)
    except Exception:
        # nothing unchecked runs: the call fails
        log.warning("guard failed", exc_info=True, extra={"event": "guard.failed",
                                                          "tool": call["name"]})
        return "error", ToolResult(content=FAILED, is_error=True)
    if out is not None and not isinstance(out, list):
        return out                     # a refusal stands; a freeze stops the turn too
    if stopped:
        return None if token.shutdown else ("cancelled", ToolResult(content="cancelled"))
    return out


async def gated(v, turn, call, store, cfg, app):
    """What a verdict means for the call: [] or advice lines (it runs), or the refusal
    (state, ToolResult). The owner hears the note, a confirm asks them, a freeze
    freezes it."""
    cid, tid = turn["conversation_id"], turn["id"]
    if v is None or v.decision in ("allow", "failed", "would_stop"):
        return []
    from . import chain, talk
    # an L1 stop or freeze gets a second look in the background (chain.py): the owner's
    # note waits for it, a freeze it says no to is lifted
    second = app is not None and v.decision == "stop" and v.row is not None and \
        v.rule in rules.REVIEWED and chain.level2(cfg) is not None and \
        chain.later(app, chain.after_stop, v.row, v.note, cid, tid)
    if v.note and app is not None and not second:
        await talk.to_owner(app, v.note, cid, tid)
    if v.decision == "asked":
        approvals = getattr(app, "approvals", None)
        try:
            no = await approvals.ask(v.ask["action"], v.ask["facts"], cid, v.ask["why"],
                                     v.ask["short"], kind="gate", ref=v.ask["rid"],
                                     call=call.get("id")) \
                if approvals is not None \
                else f"{cfg.user} did not answer"
        except anyio.get_cancelled_exc_class():
            # a Stop while they were asked: the ask is gone (approvals' finally), the row
            # is closed
            with anyio.CancelScope(shield=True):
                await rows.mark(store, v.ask["rid"], decision="closed")
            raise
        if no is None:
            await rows.mark(store, v.ask["rid"], decision="approved")
            return [bastet.fill(cfg, bastet.APPROVED, tool=call["name"])]
        late = no.startswith(f"{cfg.user} did not answer")
        denied = not late and " denied it" in no
        await rows.mark(store, v.ask["rid"], decision="denied" if denied else "closed")
        text = bastet.fill(cfg, bastet.DENIED if denied else bastet.UNANSWERED,
                           tool=call["name"]) if denied or late else \
            bastet.fill(cfg, bastet.NOT_ASKED, said=no)
        return _gated(text, call["name"], rule="confirm")
    if v.freeze and app is not None:
        # the freeze stops this turn too: it finishes (the owner hears it) all the same
        with anyio.CancelScope(shield=True):
            await freezes.freeze(app, by="guard", why=v.freeze, conversation_id=cid,
                                 turn_id=tid, decision_id=v.row)
    return _gated(v.sentence, call["name"], rule=v.rule, source=v.source, quote=v.quote,
                  why=v.why, froze=bool(v.freeze))


def _pass_lines(cfg, made: list[dict]) -> list[str]:
    """One line per pass it has not heard of: the rule, where, until when."""
    from datetime import datetime

    from ..clock import zone
    out = []
    for p in made:
        until = datetime.fromisoformat(p["until"]).astimezone(zone(cfg))
        out.append(bastet.fill(
            cfg, bastet.PASSED, rule=bastet.plain(cfg, p["rule"]),
            where="everywhere" if p["conversation_id"] is None else "here",
            until=f"{until:%H:%M}"))
    return out
