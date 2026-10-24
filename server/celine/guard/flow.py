"""The flow of one call through Bastet: the closed gates
(`closed`), then `check`: code's facts (facts.py, trigger.py), passes, the questions
judge (questions.py), the hijack judge (judge.py), the verdict and the stops
rule. No prompts here (prompts.py, questions.py), no SQL (store.py), no sentences
(bastet.py, rules.TABLE).

`on_call` is the entry point for one call with the app's judges; `check` the same with
judges given (tests, a bare runner); `closed` the closed gates before either.

- Code first (facts.py, trigger.py): a rule is asked only where code found a hard fact:
  outside text the call copies (hijack), a piece classed sensitive or nsfw it sends out
  (leak), a command or tool that breaks out (escape) or destroys what matters (damage),
  a site the task never named (sites), a tool the owner listed (confirm). No fact: no
  judge, no row.
- The judge confirms the fact: the hijack rule keeps its own two stages (judge.py, its
  deny held to a quote found in the outside text); every other rule is one question to
  questions.Judge, answered as JSON {verdict, quote, why}, a yes held to a quote found
  in the call. The row keeps the fact (piece),
  the quote and the why.
- What a rule does (`does`): watch (its row only), stop (not run, a fixed sentence with
  the judge's why; the owner hears it once per rule and evidence and conversation in a
  day), freeze (stop, and it is frozen: freeze.py). confirm asks the owner
  (celine/approvals.py). `stops`: this many stops in one turn, then its `does`.
  `persist`: after a stop in this conversation, a call code finds trying the same thing
  another way (persist.again), its first such try `first`, the next `does`.
- Passes (passes.py): a rule the owner suspended runs, its row "passed"; so does a call
  they allowed once on a banner (banners.py, where they read and answer its decisions).
- The gates (rules.gate, `closed`): plain code before every rule, in Watch and On. A
  copied private piece whose class's gate to the call's sink is closed stops the call:
  no model, no pass, no allow-once, no Watch. An `open` gate is never looked at.
- The page check (page.py) marks an outside result that talks to an AI (patterns and
  judge both): after such a mark in a turn every acting call goes to the hijack judge.
- The patrol (patrol.py) asks the rules again, after the fact, on finished turns.

The switch is the General setting `guard`: off (nothing here runs, no result is
labelled, its context is exactly as without it), watch (every call runs at once; the
rules are answered in the background and write their rows, nothing acts; froze marks
what would have frozen it), on. Read at call time, so a change applies at once.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from functools import partial

import anyio

from .. import metrics
from ..clock import zone
from ..core.ids import now
from . import banners, bastet, facts, intent, judge as hijack, passes as passes_mod, persist
from . import questions, rules as rule_set, store as decisions
from .mark import spans as spans_of
from .small_role import Unavailable
from .sources import NEVER
from .trigger import look
from .verdict import STRENGTH, Judge, Ruling, Verdict, confirm

log = logging.getLogger("celine.guard")

RETRY = 5.0          # seconds between asks while the judge cannot answer (On waits)
WATCH_LIMIT = 120.0  # seconds Watch's one ask may take; then its row is "unreachable"
WAITING = "waiting for Bastet"
TOLD_HOURS = 24      # the same rule and evidence in a conversation: one note to the owner


def _listed(tools: list[str], name: str) -> bool:
    return name in tools or name.split("__")[0] in tools


def _decision(act: str | None, mode: str) -> str:
    """A rule's row: what it did, or in Watch (or a rule set to watch) what it would."""
    if act is None:
        return "allow"
    if mode == "watch" or act == "watch":
        return "would_stop"
    return "asked" if act == "ask" else "stop"


async def _until(fn, mode: str, tool: str):
    """fn() once it answers: On asks again every RETRY seconds while it cannot (the
    call waits), Watch once within WATCH_LIMIT. -> (answer, None) or, in Watch,
    (None, why it got none). Any other error is raised."""
    while True:
        why = f"no answer in {WATCH_LIMIT:.0f} s"
        try:
            with anyio.move_on_after(WATCH_LIMIT if mode == "watch" else None):
                return await fn(), None
        except Unavailable as e:
            why = str(e) or "the judge is away"
            log.warning("guard judge away", extra={"event": "guard.judge_away",
                                                   "tool": tool, "error": why})
        if mode == "watch":
            return None, why
        await anyio.sleep(RETRY)


async def on_call(turn: dict, call: dict, request: list[dict], *, mode: str, app, store,
                  cfg, registry, progress=None) -> Verdict | None:
    """The entry point for one call, with the app's judges: the hijack judge
    (judge.ask) and the questions judge (questions.judge_for) on the app's model layer,
    behind every turn in Watch (the call already runs), at its turn's priority in On.
    -> check()'s verdict. The caller acts on it: runs the call, refuses it with
    `v.sentence`, asks the owner (`v.ask`), freezes (`v.freeze`), sends `v.note`."""
    llm = getattr(app, "llm", None)
    background = mode == "watch"
    return await check(turn, call, request, mode=mode, store=store, cfg=cfg,
                       progress=progress,
                       judge=partial(hijack.ask, llm, cfg, registry, store,
                                     background=background),
                       ask=questions.judge_for(cfg, llm, turn, background=background))


async def check(turn: dict, call: dict, request: list[dict], *, mode: str,
                judge: Judge, store, progress=None, ask=None, cfg=None,
                at: str | None = None) -> Verdict | None:
    """Bastet on one call. -> None when no rule had a reason to look (no row), else the
    verdict, its rows written. `call`: {id, name, args, description, read_only,
    message_id, sends}. `judge`: the hijack rule's judge; `ask`: the questions.Judge for
    the other rules (None: only hijack and confirm are answered);
    `cfg`: the rule set and the names in Bastet's sentences; `at`: now (persist's
    window; a test fixes it).
    `progress(note)`: told when a judge starts. On waits as long as a judge cannot
    answer (small_role.Unavailable); the caller ends the wait on a Stop. Watch asks once
    within WATCH_LIMIT; unanswered, its rows are "unreachable" and the verdict "failed".
    Any other error is raised, its rows "failed"."""
    rules = rule_set.current(cfg)
    on = {r: rules[r]["on"] for r in rule_set.RULES}
    # the page check found words aimed at an AI in this turn: the stakes are raised
    raised = await decisions.injected(store, turn["id"])
    hit = look(call["name"], call["args"], bool(call.get("read_only")),
               spans_of(request), raised=raised) if on["hijack"] else None
    sink, sent = facts.sink(call)
    found: dict[str, dict] = {}
    her = None
    # image and music prompts are never checked (image moderation is left to the ComfyUI
    # server), the vault has its own taps: only the owner's confirm list reaches them
    if ask is not None and not call["name"].startswith(NEVER):
        if on["leak"] and sink != "house" and (pieces := [
                p for p in facts.copies(await facts.private(store, turn, request), sent,
                                        await facts.outside(store, request))
                if p.cls in facts.LEAKS and rule_set.gate(rules, p.cls, sink) != "open"]):
            p = max(pieces, key=lambda x: facts.CLASSES.index(x.cls))
            found["leak"] = {"what": bastet.KINDS[p.kind], "where": p.where, "cls": p.cls,
                             "sink": bastet.sub(cfg, bastet.SINKS[sink]), "piece": p.text,
                             "pieces": pieces}
        if on["escape"] and (why := facts.escape(call)):
            found["escape"] = {"fact": why}
        if on["damage"] and (why := facts.damage(call)):
            found["damage"] = {"fact": why}
        if on["sites"] and facts.typed((call.get("args") or {}).get("steps")):
            her = await intent.read(store, turn, call)
            if site := await facts.site(store, turn, call, her):
                found["sites"] = {"site": site}
        # persist reads past stops only for a call that could repeat one
        if on["persist"] and persist.can_again(call, sink) and (same := await _again(store, turn, call, sink, sent, mode,
                                                   rules["persist"], cfg, at)):
            found["persist"] = same
    if on["confirm"] and _listed(rules["confirm"]["tools"], call["name"]):
        found["confirm"] = {}
    if hit is None and not found:
        return None

    cid = turn["conversation_id"]
    held = await passes_mod.holding(store, cid)
    # the owner allowed this very call once (their answer on a banner): every rule passes.
    # Not in Watch: its check runs after the call ran and would eat the mark
    once = mode != "watch" and await banners.take_once(
        store, cid, call["name"], call.get("args") or {})
    answers: list[Ruling] = []
    for name in [*found, *(["hijack"] if hit else [])]:
        if not once and passes_mod.covers(held, name) is None:
            continue
        f = found.pop(name, {})
        if name == "hijack":
            hit = None
        await decisions.record(store, turn=turn, call=call, mode=mode, rule=name,
                               does=rules[name].get("does"), decision="passed", layer=1,
                               cls=f.get("cls"), sink=sink if name == "leak" else None,
                               piece=f.get("piece"))
        metrics.record_guard("passed", name, 0, mode=mode)
    if hit is None and not found:
        return Verdict("allow", None, rule="passed")

    if progress is not None and (hit is not None or set(found) & set(rule_set.ASKED)):
        await progress(WAITING)
    v = Verdict("allow", hit)
    # the questions: one ask for every rule code found a reason for
    names = [r for r in rule_set.ASKED if r in found]
    if names:
        her = her if her is not None else await intent.read(store, turn, call)
        state = questions.call_state(call, sink, her, found)
        try:
            got, why = await _until(lambda: ask(state, questions.asked(cfg, names)), mode,
                                    call["name"])
        except Exception as e:
            for r in names:
                await _row(store, turn, call, mode, rules, r, "failed", found[r],
                           sink, why=type(e).__name__)
            raise
        if got is None:
            for r in names:
                await _row(store, turn, call, mode, rules, r, "unreachable", found[r], sink,
                           why=why)
            metrics.record_guard("unreachable", names[0], 0, mode=mode)
            if hit is None and "confirm" not in found:
                return Verdict("failed", None, why=why)
        else:
            for r in names:
                p = float(got.p.get(r, 0.0))
                # persist: `first` on the first try at a stopped thing, `does` after
                does = rules[r]["first"] if r == "persist" and found[r]["first"] \
                    else rules[r]["does"]
                act = does if got.yes(r) else None
                quote, why = got.quote.get(r, ""), got.why.get(r, "")
                rule = Ruling(r, act, found[r], p, quote=quote, why=why)
                rule.rid = await _row(store, turn, call, mode, rules, r, _decision(act, mode),
                                      found[r], sink, p=p, model=got.model, ms=got.ms,
                                      does=does, why=why, quote=quote)
                metrics.record_guard(_decision(act, mode), r, got.ms, mode=mode)
                answers.append(rule)
    if "confirm" in found:
        act = "ask"
        answers.append(Ruling("confirm", act, {}, None, await _row(
            store, turn, call, mode, rules, "confirm", _decision(act, mode), {}, sink,
            layer=1)))
    if hit is not None:
        h = await _hijack(turn, call, hit, mode, judge, store, rules)
        if h is None:                                   # Watch, the judge never answered
            v.decision, v.why = "failed", "no answer"
        else:
            v.source, v.quote, v.why, v.stops = h.source, h.quote, h.why, h.stops
            if h.act is not None:
                answers.append(h)
    return await _verdict(v, answers, rules, mode, store, turn, call, cfg)


async def closed(turn: dict, call: dict, request: list[dict], *, mode: str, store,
                 cfg) -> Verdict | None:
    """The closed gates, before any rule and the same in Watch and On: a private piece
    copied into the call whose class's gate to the call's sink is closed. -> a stop
    with Bastet's plain line, its row decision "closed" (rule `gates`); None when no
    closed gate holds the call. No model is asked; passes and allow-once are never read.
    Image and music prompts and the vault are never looked at (NEVER)."""
    rules = rule_set.current(cfg)
    sink, sent = facts.sink(call)
    shut = {c for c in rule_set.GATED if rule_set.gate(rules, c, sink) == "closed"}
    if not shut or call["name"].startswith(NEVER) or not "".join(sent).strip():
        return None
    corpus = [p for p in await facts.private(store, turn, request) if p.cls in shut]
    pieces = facts.copies(corpus, sent, await facts.outside(store, request))
    if not pieces:
        return None
    p = max(pieces, key=lambda x: facts.CLASSES.index(x.cls))
    rid = await decisions.record(store, turn=turn, call=call, mode=mode, rule="gates",
                                 does="closed", decision="closed", layer=1, cls=p.cls,
                                 sink=sink, piece=p.text)
    metrics.record_guard("closed", "gates", 0, mode=mode)
    log.info("guard gate closed", extra={"event": "guard.closed", "tool": call["name"]})
    return Verdict("stop", None, rule="gates", row=rid, text=bastet.fill(
        cfg, bastet.STOPPED, said=bastet.sub(cfg, rule_set.TABLE["gates"].to_her), why="",
        then=bastet.CARRY))


async def _hijack(turn, call, hit, mode, judge, store, rules):
    """The hijack rule through its own judge and the evidence rule. -> its Ruling (with
    source, quote, why, stops), None when Watch got no answer."""
    first = hit.spans[0].source if hit.spans else None
    does = rules["hijack"]["does"]
    rid = await decisions.begin(store, turn=turn, call=call, mode=mode, link=hit.link,
                                source=first, does=does)
    took = time.monotonic()
    try:
        j, why = await _until(lambda: judge(turn, call, hit), mode, call["name"])
    except Exception as e:
        await _failed(store, rid, type(e).__name__, took)
        raise
    if j is None:
        await _failed(store, rid, why, took, "unreachable")
        metrics.record_guard("unreachable", first or "none", 0, mode=mode)
        return None
    judged = time.monotonic()
    held, unheld = confirm(j.quote, hit.spans, call) if j.deny else (None, "")
    act = does if held is not None else None
    decision = _decision(act, mode)
    source = held.source if held else first
    r = Ruling("hijack", act, {"source": source or "outside"}, (j.scores or {}).get("p_deny"),
              rid, source, " ".join(j.quote.split()) if held else "", j.why if held else "")
    r.stops = await decisions.settle(
        store, rid, decision=decision, source=r.source, quote=r.quote,
        why="; ".join(w for w in (unheld, j.why) if w),
        scores=j.scores, model=j.model, ms=round((time.monotonic() - took) * 1000), p=r.p)
    metrics.record_guard(decision, r.source or "none", (judged - took) * 1000, mode=mode)
    log.info("guard decided", extra={"event": f"guard.{decision}", "tool": call["name"],
                                     "link": hit.link})
    return r


async def _verdict(v: Verdict, answers: list[Ruling], rules, mode, store, turn, call,
                   cfg) -> Verdict:
    """The strongest act decides; hijack freezes when leak or damage also fired; the
    stops rule counts this turn's stops."""
    def said(template: str, r: Ruling) -> str:
        return bastet.fill(cfg, template, **{k: v for k, v in r.facts.items()
                                             if isinstance(v, (str, int))})
    acting = [r for r in answers if r.act not in (None, "watch")]
    fired = {r.name for r in acting}
    for r in acting:
        if r.act == "stop" and rule_set.escalates(r.name, fired):
            r.act = "freeze"
    if mode == "watch":
        for r in acting:
            if r.act == "freeze" and r.rid is not None:
                await decisions.mark(store, r.rid, froze=True)
        if any(r.act in ("stop", "freeze", "ask") for r in acting):
            v.decision = "would_stop"
            await _stops_rule(v, rules, mode, store, turn, call, cfg)
        elif v.decision != "failed":
            v.decision = "allow"
        return v
    if not acting:
        return v
    # persist speaks first on a tie: "I said no" is the plainest word on a try again
    top = max(acting, key=lambda r: (STRENGTH[r.act], r.name == "persist"))
    v.rule, v.row = top.name, top.rid
    if top.act in ("stop", "freeze"):
        v.decision = "stop"
        if top.name != "hijack":
            v.stops = await decisions.stops(store, turn["id"])
        if top.act == "freeze":
            v.freeze = _owner(cfg, call, said(rule_set.TABLE[top.name].to_owner, top), top)
            if top.rid is not None:
                await decisions.mark(store, top.rid, froze=True)
        else:
            await _stops_rule(v, rules, mode, store, turn, call, cfg)
            if not v.note and not v.freeze and await _fresh(store, turn, top):
                v.note = bastet.fill(cfg, bastet.NOTE, tool=bastet.tool_words(call["name"]),
                                     said=said(rule_set.TABLE[top.name].to_owner, top),
                                     why=_why(top), done=bastet.sub(cfg, bastet.DONE["stop"]))
        # it reads that it is frozen, else that it carries on
        then = bastet.sub(cfg, bastet.FROZEN_NOW) if v.freeze else bastet.CARRY
        if top.name == "hijack":
            v.text = v.hijacked(then)
        else:
            # a try again that another rule stops too is still "I said no"
            again = next((r for r in answers if r.name == "persist" and r.act is not None),
                         None)
            v.text = bastet.fill(cfg, bastet.STOPPED, said=said(rule_set.TABLE[top.name].to_her, top),
                                 why=_why(top), then=then) if again is None else \
                bastet.fill(cfg, bastet.AGAIN, said=said(rule_set.TABLE["persist"].to_her, again),
                            why=_why(again), then=then)
        return v
    if top.act == "ask":
        v.decision = "asked"
        args = call.get("args") or {}
        v.ask = {"action": bastet.ASK.format(tool=call["name"]),
                 "facts": [f"{k}: {str(x)[:200]}" for k, x in args.items()],
                 "why": bastet.sub(cfg, rule_set.TABLE["confirm"].to_her),
                 "short": call["name"], "rid": top.rid}
    return v


def _why(r: Ruling) -> str:
    """The judge's sentence, as the next words of a line ("" when there is none)."""
    return bastet.sentence(r.why)


def _owner(cfg, call: dict, said: str, r: Ruling | None = None) -> str:
    """Why it is frozen, in plain words: what it did, on which tool, the judge's why."""
    return bastet.FROZEN_FOR.format(said=said, tool=bastet.tool_words(call["name"]),
                                    why=_why(r) if r else "").strip()


async def _fresh(store, turn: dict, r: Ruling) -> bool:
    """No note yet to the owner on this rule and evidence in this conversation within
    TOLD_HOURS."""
    if r.rid is None:
        return True
    return not await decisions.told(store, turn["conversation_id"], r.rid, TOLD_HOURS)


async def _stops_rule(v: Verdict, rules, mode, store, turn, call, cfg) -> None:
    """The stops rule: when this stop is the turn's `count`th, its `does`."""
    r = rules["stops"]
    if not r["on"]:
        return
    n = await decisions.stops(store, turn["id"], "would_stop" if mode == "watch" else "stop")
    v.stops = n if mode == "on" else v.stops
    if n != r["count"]:
        return
    act = r["does"]
    if passes_mod.covers(await passes_mod.holding(store, turn["conversation_id"]), "stops"):
        await decisions.record(store, turn=turn, call=call, mode=mode, rule="stops",
                               does=act, decision="passed", layer=1)
        return
    rid = await decisions.record(store, turn=turn, call=call, mode=mode, rule="stops",
                                 does=act, decision=_decision(act, mode), layer=1)
    if act == "freeze":
        await decisions.mark(store, rid, froze=True)
    if mode == "watch":
        return
    said = bastet.fill(cfg, rule_set.TABLE["stops"].to_owner, count=n)
    if act == "freeze":
        v.freeze, v.rule, v.row = _owner(cfg, call, said), "stops", rid
    elif act == "stop":
        v.note = bastet.fill(cfg, bastet.NOTE, tool=bastet.tool_words(call["name"]),
                             said=said, why="", done=bastet.sub(cfg, bastet.DONE["stop"]))


async def _row(store, turn, call, mode, rules, rule, decision, f, sink, *, why="",
               p=None, model=None, ms=None, layer=2, does=None, quote="") -> int:
    return await decisions.record(
        store, turn=turn, call=call, mode=mode, rule=rule,
        does=does or rules[rule].get("does"),
        decision=decision, layer=layer, why=why, quote=quote, cls=f.get("cls"),
        sink=sink if rule == "leak" else None,
        piece=f.get("piece") or f.get("fact") or f.get("site"), p=p, model=model, ms=ms)


async def _again(store, turn, call, sink, sent, mode, rule, cfg, at) -> dict | None:
    """persist: the stop in this conversation within the rule's `minutes` that this call
    tries again another way (persist.again), newest first. -> its facts (`first`: no try
    at that stop, the same thing, was acted on yet), None when nothing is the same."""
    t = at or now()
    since = (datetime.fromisoformat(t) - timedelta(minutes=rule["minutes"])).isoformat(
        timespec="milliseconds")
    watch = mode == "watch"
    cid = turn["conversation_id"]
    for prev in await decisions.stopped(store, cid, since, t, call["id"],
                                        "would_stop" if watch else "stop"):
        if same := persist.again(prev, call, sink, sent):
            tried = await decisions.retries(
                store, cid, prev["created_at"], same,
                ("stop", "would_stop"))
            when = datetime.fromisoformat(prev["created_at"]).astimezone(zone(cfg))
            return {"what": bastet.sub(cfg, rule_set.plain(prev["rule"])), "same": same,
                    "piece": same, "prev": prev["rule"], "tool": prev["tool"],
                    "at": f"{when:%H:%M}", "first": tried == 0}
    return None


async def _failed(store, rid, why: str, took: float, decision: str = "failed") -> None:
    await decisions.settle(store, rid, decision=decision, source=None, quote="", why=why,
                           scores=None, model=None, ms=round((time.monotonic() - took) * 1000))
