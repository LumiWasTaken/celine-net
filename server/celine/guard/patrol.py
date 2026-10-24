"""Bastet's patrol: at a random time
within guard.toml [patrol] every_minutes it picks a few turns that finished since the
last patrol, at random (`turns`; chats, missions, routines, sub-agents alike). A
background job of the app, no turn of its own.

The firm flow, after the fact: code reads the turn's calls for hard facts (`found`:
facts.escape and facts.damage on each call; hijack: an acting call after a result the
page check marked). No fact: nothing is asked. A rule with a fact asks the judge its
questions.toml [after] question with the facts; a finding is the judge's yes with a
quote found in the turn's steps. Leak is not asked here: the gate holds the private
pieces, the patrol never reads its notes.

What the judge sees of a turn (intent.finished): its intent, its replies, its calls with
their arguments, where each result came from and what the page check marked. Never a
result's text and never its notes (notebook and mind calls are left out whole).

A finding is one decision row (rule, `does` "patrol", decision found, tool "patrol" on
the turn's id, message_id its last reply there; piece the fact, quote, why). The
findings of a patrol go to the owner in one Intercom report from Bastet, each in plain
words with the judge's why; a finding already told in that conversation within a day
is not told again. No finding: no message. A finding whose rule does freeze (hijack too when leak or damage was found on
the same turn, like the gate) freezes it in mode On only; in Watch its row is marked
froze.

It runs only while the guard is watch or on, [patrol] on, and it is not frozen; the
file is read at run time. kv "patrol_at" holds when the last patrol read. A judge away
keeps it, so the next patrol reads those turns again; a turn with a finding already is
not picked again, so no finding is reported twice.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime

import anyio

from ..clock import zone
from ..core.ids import now
from . import bastet, chain, facts, freeze as freeze_mod, questions, rules as rule_set
from . import store as decisions
from .intent import finished
from .small_role import Unavailable
from .switch import mode as guard_mode
from .talk import voiced

log = logging.getLogger("celine.guard")

TOLD_HOURS = 24      # a finding already told in its conversation within this: not again
READS = ("web__search", "web__read_page", "browser__open")
FINISHED = ("done", "capped")


async def run(app, rng: random.Random | None = None) -> None:
    """The app's loop: one patrol at a random time in each stretch of about
    every_minutes (half to one and a half of it), read again each time. The first
    stretch counts from the last patrol (kv patrol_at), so a restart does not reset it."""
    rng = rng or random.Random()
    left = _since(await decisions.patrol_at(app.store), now())
    while True:
        every = rule_set.current(app.cfg)["patrol"]["every_minutes"]
        await anyio.sleep(max(0.0, every * 60 * rng.uniform(0.5, 1.5) - left))
        left = 0.0
        try:
            await once(app, rng=rng)
        except Exception:
            log.warning("patrol failed", exc_info=True, extra={"event": "guard.patrol_failed"})


def _since(at: str, t: str) -> float:
    """Seconds from the last patrol read (`at`) to `t`, 0 when there was none."""
    if not at:
        return 0.0
    return max(0.0, (datetime.fromisoformat(t) - datetime.fromisoformat(at))
               .total_seconds())


async def once(app, *, rng: random.Random | None = None, at: str | None = None,
               judge=None) -> dict | None:
    """One patrol now (`at`). -> {read, findings}, None when it did not run. `judge`:
    the questions.Judge (default: the one guard.toml names, background priority). A
    judge away ends the patrol and keeps patrol_at: the next one reads those turns
    again."""
    cfg, store = app.cfg, app.store
    rules = rule_set.current(cfg)
    gm = guard_mode(cfg)
    if gm == "off" or not rules["patrol"]["on"] or await freeze_mod.frozen(store):
        return None
    names = [r for r in rules["patrol"]["rules"] if rules[r]["on"]]
    rng = rng or random.Random()
    t = at or now()
    done = await decisions.finished(store, await decisions.patrol_at(store), t, FINISHED)
    picked = rng.sample(done, min(len(done), rules["patrol"]["turns"])) if names else []
    read, found, away = 0, [], False
    for turn in picked:
        if away:
            break
        seen = await store.read(lambda c: finished(c, turn))
        if seen is None:
            continue
        state, last, title, calls = seen
        read += 1
        code = {r: f for r, f in found_in(calls).items() if r in names}
        ask = judge or questions.judge_for(cfg, getattr(app, "llm", None), turn,
                                           background=True)
        hits = {}
        for r, fact in code.items():
            try:
                got = await ask({**state, "facts": [fact]}, questions.asked(cfg, [r], "after"))
            except Unavailable as e:
                log.info("patrol judge away", extra={"event": "guard.patrol_away",
                                                     "error": str(e)})
                away = True
                break
            if not got.yes(r):
                continue
            rid = await decisions.record(
                store, turn=turn, call={"id": turn["id"], "name": "patrol",
                                        "message_id": last},
                mode=gm, rule=r, does="patrol", decision="found", p=got.p[r],
                model=got.model, ms=got.ms, piece=fact, quote=got.quote[r],
                why=got.why.get(r, ""))
            # the second look before the owner hears of it; its no is not a finding
            said = await chain.review(app, rid)
            if said == "no":
                await decisions.mark(store, rid, decision="allow")
                continue
            why = got.why.get(r, "")
            if said == "unreachable":
                why = f"{bastet.sentence(why)}{await chain.unanswered(app, rid)}"
            hits[r] = (rid, why)
        for r, (rid, why) in hits.items():
            froze = rules[r]["does"] == "freeze" or rule_set.escalates(r, set(hits))
            if froze:
                await decisions.mark(store, rid, froze=True)
            told = await decisions.told(store, turn["conversation_id"], rid, TOLD_HOURS)
            found.append({**turn, "rule": r, "row": rid, "froze": froze, "title": title,
                          "why": why, "told": told})
    if not away:
        await decisions.patrolled(store, t)
    log.info("patrolled", extra={"event": "guard.patrol", "read": read,
                                 "findings": len(found)})
    if news := [f for f in found if not f["told"] or f["froze"]]:
        await _report(app, gm, read, news)
    return {"read": read, "findings": found}


async def _report(app, gm: str, read: int, found: list[dict]) -> None:
    """One Intercom report to the owner; then the freeze, in On, when one froze it."""
    cfg = app.cfg
    tz = zone(cfg)
    stop = next((f for f in found if f["froze"]), None)
    lines = []
    for f in found:
        when = datetime.fromisoformat(f["finished_at"]).astimezone(tz)
        lines.append(bastet.fill(
            cfg, bastet.FINDING, title=f["title"], when=f"{when:%A %H:%M}",
            said=bastet.sub(cfg, rule_set.TABLE[f["rule"]].found), why=bastet.sentence(f["why"]),
            froze="" if not f["froze"] else
            bastet.sub(cfg, "I froze {{them}}." if gm == "on" else "I would have frozen {{them}}.")).strip())
    first = stop or found[0]
    text = await voiced(app, "\n".join(lines), first["conversation_id"])
    await bastet.tell_owner(app, text, first["conversation_id"], first["id"])
    if stop is not None and gm == "on":
        await freeze_mod.freeze(
            app, by="guard", why=bastet.fill(cfg, "looking back at {title}: {said}. {why}",
                            title=stop["title"], why=stop["why"],
                            said=bastet.sub(cfg, rule_set.TABLE[stop["rule"]].found)).strip(),
            conversation_id=stop["conversation_id"], turn_id=stop["id"],
            decision_id=stop["row"])


def found_in(calls: list[dict]) -> dict[str, str]:
    """Code's hard facts over a finished turn's calls, the first per rule: escape and
    damage on each call; hijack, an acting call after a result the page check marked."""
    out: dict[str, str] = {}
    marked = None
    for call in calls:
        tool = call["name"]
        for rule, fn in (("escape", facts.escape), ("damage", facts.damage)):
            if rule not in out and (why := fn(call)):
                out[rule] = f"{tool}: {why}"
        # reading more is never acting on an order: a search, a page, a tab opened
        if marked and "hijack" not in out and (
                (facts.sink(call)[0] != "house" and tool not in READS)
                or facts.damage(call) or facts.escape(call)):
            out["hijack"] = f"{tool} after words aimed at an AI in {marked}"
        if isinstance(call.get("display", {}).get("injection"), dict):
            marked = (call["display"].get("outside") or {}).get("source") or tool
    return out
