"""Bastet's page check: every outside
result (sources.label) is checked for text aimed at an AI as it is received, in its turn
or from a job.

- The plain patterns (patterns.py) always; the judge's question "injection"
  (questions.Judge, a yes held to a quote from the page) only on a result the patterns
  flag. Both must say yes: a judge that cannot answer (away, or slower than LIMIT)
  marks nothing.
- A hit marks the result's display (`injection`: {p, quote}) and writes one row (rule
  injection, does "mark", decision allow: no banner; the patterns' words as its piece,
  the judge's quote and why). Nothing
  else: no line to it, no note to the owner. No hit: no row.
- A mark raises the stakes for the rest of that turn (guard.check reads
  store.injected): every acting call goes to the hijack judge.
- It never blocks reading: the result always reaches it.
"""
from __future__ import annotations

import logging
from dataclasses import replace

import anyio

from . import passes as passes_mod, patterns, questions, rules as rule_set
from . import store as decisions
from .small_role import Unavailable

log = logging.getLogger("celine.guard")

LIMIT = 15.0         # seconds the judge may take on one result; slower marks nothing
CHARS = 8000         # characters of the result the judge reads


async def check(result, *, turn: dict, call: dict, store, cfg, llm, mode: str,
                app=None):
    """`result` (a ToolResult, labelled already) as it will read it: marked on a hit,
    else as it came. `call`: {id, name, message_id}. Never
    raises."""
    label = (result.display or {}).get("outside")
    rule = rule_set.current(cfg)["injection"]
    if not isinstance(label, dict) or not rule["on"] or not result.content:
        return result
    try:
        return await _check(result, label, rule, turn, call, store, cfg, llm, mode, app)
    except Exception:
        log.warning("page check failed", exc_info=True,
                    extra={"event": "guard.page_failed", "tool": call["name"]})
        return result


async def _check(result, label, rule, turn, call, store, cfg, llm, mode, app):
    quote = patterns.aimed(result.content) or ""
    if not quote:
        return result
    source = label.get("source") or "outside"
    if passes_mod.covers(await passes_mod.holding(store, turn["conversation_id"]),
                         "injection"):
        return result
    got = None
    with anyio.move_on_after(LIMIT):
        try:
            judge = questions.judge_for(cfg, llm, turn, background=mode == "watch")
            got = await judge({"page": result.content[:CHARS], "source": source},
                              questions.asked(cfg, ["injection"]))
        except Unavailable as e:
            log.info("page check without the judge", extra={
                "event": "guard.page_judge_away", "tool": call["name"], "error": str(e)})
    if got is None or not got.yes("injection"):
        return result
    p = float(got.p["injection"])
    said = got.quote.get("injection") or quote
    await decisions.record(
        store, turn=turn, call=call, mode=mode, rule="injection", does="mark",
        decision="allow", layer=2, source=source, quote=said, piece=quote,
        why=got.why.get("injection", ""), p=p, model=got.model, ms=got.ms)
    d = {**(result.display or {}), "injection": {"p": p, "quote": said}}
    return replace(result, display=d)
