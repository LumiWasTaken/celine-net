"""The escalation chain: who looks at a finding, in order.

- L0 code (facts.py): no fact, nothing asked.
- L1 the guard role (questions.SmallRole, the hijack judge): JSON {verdict, quote, why},
  a yes held to a quote. A no ends it.
- L2 a second look by a big reasoning model, only on an L1 yes that stopped, froze or
  would have (flow), and on a patrol finding before the owner hears of it. guard.toml
  [chain] names it: `provider` (a providers.toml provider) and `model`; empty provider:
  no L2. Asked through the app's model layer like every other call (llm.make, then
  llm.bind at background priority, exactly that model, no fallback): it waits in the
  queue behind the turns, its errors are the provider's words. `seconds` the most it
  may take, its wait for the model included; `thinking` "off" (the default) or a level,
  as a chat sends it to that provider's kind (llm.make).

What L2 decides: "yes" (a yes with a quote found in the evidence) confirms L1; "no"
overrules it: a stop's note to the owner is not sent, a freeze L1 made is lifted by
Bastet herself (owner.second_look), a patrol finding is not reported. When L2 is not
configured, not reachable or slower than `seconds`, L1's verdict stands, the row
says so and so does the owner's note or patrol finding (`unanswered`). Every level's verdict and why goes on the row (scores "chain"); Events shows
it.
"""
from __future__ import annotations

import json
import logging
import time

import anyio

from ..backends.base import BackendError, Done, ReasoningDelta, TextDelta
from ..backends.queue import Priority
from ..settings.thinking import OFF
from . import bastet, caps, freeze as freezes, intent, prompts, rules as rule_set
from . import store as decisions, talk
from .owner import second_look
from .small_role import Unavailable, parse_json
from .trigger import strings
from .verdict import confirm

log = logging.getLogger("celine.guard")
# the answer is one short JSON (quote and why at most 300 characters each); with thinking
# on, room to think first
TOKENS, THINKING_TOKENS = 512, 4096


def level2(cfg) -> dict | None:
    """{ref, model, seconds, thinking} of L2, None when [chain] names no provider or the
    provider does not exist."""
    ch = rule_set.current(cfg)["chain"]
    pid, model = ch["provider"].strip(), ch["model"].strip()
    s = getattr(cfg, "settings", None)
    if not pid or not model or pid not in (getattr(s, "providers", None) or {}):
        return None
    return {"ref": f"{pid}/{model}", "model": model, "seconds": float(ch["seconds"]),
            "thinking": ch["thinking"] or OFF}


def _answer(said: dict) -> dict | None:
    """L2's JSON answer: in what it wrote, else in its reasoning (a template or parser
    that put the answer there); None when neither holds one with a Y or N verdict."""
    for key in ("content", "reasoning"):
        v = parse_json(said.get(key) or "")
        if str(v.get("verdict") or "").strip().upper()[:1] in ("Y", "N"):
            return v
    return None


async def ask(llm, l2: dict, text: str) -> dict:
    """One L2 answer: {verdict, quote, why, model, ms}. Raises Unavailable when it cannot
    answer in time, with the provider's words on an error."""
    backend = llm.make(l2["ref"], l2["thinking"]) if llm is not None else None
    if backend is None:
        raise Unavailable(f"{l2['model']} is not a saved model of its provider.")
    # a judge: low temperature; no response_format: tabbyAPI's JSON mode can mangle the
    # keys ("/verdict/")
    qb = llm.bind(backend, role="guard", priority=Priority.BACKGROUND, fallback=False,
                  voice=False, label="Guard",
                  body={"temperature": 0.2, "top_p": 0.95, "top_k": 20,
                        "max_tokens": TOKENS if l2["thinking"] == OFF else THINKING_TOKENS})
    said: dict[str, list[str]] = {"content": [], "reasoning": []}
    took = time.monotonic()
    try:
        with anyio.fail_after(l2["seconds"]):
            events = qb.stream([{"role": "user", "content": text}], [])
            try:
                async for ev in events:
                    if isinstance(ev, TextDelta):
                        said["content"].append(ev.text)
                    elif isinstance(ev, ReasoningDelta):
                        said["reasoning"].append(ev.text)
                    elif isinstance(ev, Done):
                        break
            finally:
                await events.aclose()
    except TimeoutError:
        raise Unavailable(f"{l2['model']} took longer than {l2['seconds']:.0f} s") from None
    except BackendError as e:
        raise Unavailable(str(e)[:300]) from None
    v = _answer({k: "".join(x) for k, x in said.items()})
    if v is None:
        raise Unavailable(f"{l2['model']} gave no decision")
    return {"verdict": str(v.get("verdict") or "").strip().upper()[:1],
            "quote": " ".join(str(v.get("quote") or "").split())[:300],
            "why": " ".join(str(v.get("why") or "").split())[:300],
            "model": l2["model"], "ms": round((time.monotonic() - took) * 1000)}


def _evidence(c, row) -> tuple[str, str]:
    """(what L2 reads of the action, the text its quote must be found in)."""
    if row["does"] == "patrol":
        turn = {"id": row["tool_call_id"], "conversation_id": row["conversation_id"]}
        seen = intent.finished(c, turn)
        steps = "\n".join(f"- {s}" for s in (seen[0]["steps"] if seen else ()))
        return f"What the agent did in the turn:\n{steps}", steps
    args = decisions.call_args(c, row["tool_call_id"])
    return f"The action: {row['tool']} {caps.args(args)}", "\n".join(strings(args))


def frame(cfg, c, row) -> tuple[str, str]:
    """(L2's prompt for one row, the evidence its quote must be found in)."""
    qs = prompts.questions()
    question = cfg.substitute(qs[decisions.table_of(row)].get(row["rule"]) or qs["after"].get(
        row["rule"], "Is this action a real problem?"))
    action, seen = _evidence(c, row)
    try:
        stated = intent.build(c, {"id": row["turn_id"], "conversation_id": row["conversation_id"]},
                           {"message_id": row["message_id"]})
    except KeyError:
        stated = "(none)"
    text = cfg.substitute(prompts.text("review"), braces=True).format(
        intent=caps.tail(stated, caps.INTENT), action=action, fact=row["piece"] or "(none)",
        quote=row["quote"] or "(none)", why=row["why"] or "(none)", question=question)
    return text, seen


async def review(app, rid: int) -> str:
    """L2 on decision row `rid`. -> "yes", "no", "unreachable" or "off"; its entry is on
    the row either way (store.chain)."""
    cfg, store = app.cfg, app.store
    l2 = level2(cfg)
    if l2 is None:
        await decisions.chain(store, rid, {"level": "L2", "verdict": "off"})
        return "off"
    row = await store.read(lambda c: decisions.row(c, rid))
    if row is None:
        return "off"
    text, seen = await store.read(lambda c: frame(cfg, c, row))
    try:
        got = await ask(getattr(app, "llm", None), l2, text)
    except Unavailable as e:
        log.info("L2 away", extra={"event": "guard.l2_away", "error": str(e)})
        await decisions.chain(store, rid, {"level": "L2", "verdict": "unreachable",
                                           "why": str(e)[:200]})
        return "unreachable"
    yes = got["verdict"] == "Y" and confirm(got["quote"], seen)[0] is not None
    await decisions.chain(store, rid, {"level": "L2", **got, "verdict": "Y" if yes else "N"})
    return "yes" if yes else "no"


async def _last(app, rid: int) -> dict:
    """The newest level's entry on row `rid` (store.chain)."""
    row = await app.store.read(lambda c: decisions.row(c, rid))
    return (json.loads(row["scores"] or "{}").get("chain") or [{}])[-1]


async def unanswered(app, rid: int) -> str:
    """The owner's sentence when the second look on row `rid` did not answer: L1 stands."""
    why = (await _last(app, rid)).get("why") or "It gave no answer."
    # the provider's words as they came, with a stop after them
    return bastet.fill(app.cfg, bastet.SECOND_LOOK_AWAY,
                       why=why if why.endswith((".", "!", "?")) else why + ".")


async def after_stop(app, rid: int, note: str, conversation_id: str, turn_id: str) -> str:
    """An L1 stop or freeze in its turn, looked at again in the background: the owner's
    note goes out unless L2 says no, with why when L2 did not answer; a freeze L2 says no
    to is lifted, with its why."""
    said = await review(app, rid)
    if said == "no":
        held = await freezes.frozen(app.store)
        if held is not None and held.get("decision_id") == rid:
            by = await app.store.read(lambda c: second_look(c, rid))
            await freezes.unfreeze(app, by=by)
            last = await _last(app, rid)
            await talk.to_owner(app, bastet.fill(app.cfg, bastet.SECOND_LOOK,
                                                 why=bastet.sentence(last.get("why"))),
                                conversation_id, turn_id)
        return said
    if note:
        if said == "unreachable":
            note = f"{note} {await unanswered(app, rid)}"
        await talk.to_owner(app, note, conversation_id, turn_id)
    return said


def later(app, fn, *args) -> bool:
    """Run `fn(app, *args)` in the app's background nursery (never in its turn; the work
    queue waits out a freeze, a second look must not). False: no nursery (a script)."""
    tg = getattr(getattr(getattr(app, "dispatcher", None), "jobs", None), "tg", None)
    if tg is None:
        return False

    async def run() -> None:
        try:
            await fn(app, *args)
        except Exception:
            log.warning("second look failed", exc_info=True,
                        extra={"event": "guard.l2_failed"})
    tg.start_soon(run)
    return True
