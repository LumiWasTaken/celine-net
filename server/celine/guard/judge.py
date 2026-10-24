"""The hijack judge: the small role (guard/small_role.py) looks at one call layer 1
picked and answers: did this action come from an order in text someone else wrote, or
from it?

Two stages. Stage 1 asks for one letter, A (allow) or D (deny), and reads P(D) from
the logprobs of that token (Ollama's /api/chat with `logprobs`; another server that
sends none: the letter it wrote counts as 0 or 1). Below THRESHOLD the call runs.
Above it, stage 2 asks the same prompt (the server's prompt cache holds it) for JSON
with the exact words of the order. The deny counts only through the evidence rule
(verdict.confirm, in flow.py): the quote is in the outside text and the call carries it
out. The model cannot stop it by guessing.

Thinking off, temperature 0. It raises small_role.Unavailable when the model cannot
answer; the call then waits and asks again (flow.check).
"""
from __future__ import annotations

import math
import time

from . import caps, intent, patterns, prompts
from .mark import Span, wrap
from .small_role import complete, parse_json
from .trigger import Hit
from .verdict import Judgement

THRESHOLD = 0.2          # P(D) from stage 1 above which stage 2 is asked
AROUND = 1500            # characters of outside text kept around each linked piece
MOST = 10000             # characters of outside text in all, about 2500 tokens
STAGE2_TOKENS = 300


# ---- the inputs ------------------------------------------------------------------------

def skills(registry, cfg, loaded) -> str:
    """One line per loaded skill: what it can do."""
    lines = []
    for n in sorted(loaded or ()):
        s = registry.skills.get(n) if registry is not None else None
        if s is None:
            continue
        desc = " ".join(cfg.substitute(str(getattr(s, "description", "") or "")).split())
        lines.append(f"- {n}: {desc[:200]}")
    return "\n".join(lines) or "- (none listed)"


def excerpts(hit: Hit) -> list[tuple[Span, str]]:
    """The linked outside spans, AROUND characters each side of the piece the call
    copies (or of words aimed at an AI, else the start), newest first, MOST in all."""
    out: list[tuple[Span, str]] = []
    left = MOST
    for s in reversed(hit.spans):
        if left <= 0:
            break
        text = s.text
        if len(text) > 2 * AROUND:
            at = text.casefold().find(hit.piece.casefold()) if hit.piece else -1
            if at < 0 and (aimed := patterns.aimed(text)):
                at = text.find(aimed)
            c = max(at, 0)
            lo = max(0, c - AROUND)
            text = text[lo:lo + 2 * AROUND]
        out.append((s, text[:left]))
        left -= len(text)
    return list(reversed(out))


def prompt(cfg, registry, turn: dict, call: dict, hit: Hit, her: str) -> str:
    outside = "\n".join(wrap(t, {"source": s.source, "by": s.by}) for s, t in excerpts(hit))
    desc = " ".join(str(call.get("description") or "").split("\n")[0].split())[:200]
    # its name and the owner's go into the fixed text before the rest: the outside
    # text stays as written
    return cfg.substitute(prompts.text("hijack"), braces=True).format(
        intent=her, skills=skills(registry, cfg, turn.get("loaded")),
        outside=outside, tool=call["name"], description=desc.replace('"', "'"),
        arguments=caps.args(call.get("args")))


# ---- the model -------------------------------------------------------------------------

def p_deny(raw: dict | None, said: str) -> float:
    """P(D) against P(A) from the first token's top logprobs, else the letter said."""
    tops = []
    for lp in (raw or {}).get("logprobs") or ():
        tops = lp.get("top_logprobs") or [lp]
        break
    a = sum(math.exp(t["logprob"]) for t in tops if t.get("token", "").strip().upper() == "A")
    d = sum(math.exp(t["logprob"]) for t in tops if t.get("token", "").strip().upper() == "D")
    if a + d > 0:
        return d / (a + d)
    return 1.0 if said.strip().upper().startswith("D") else 0.0


async def ask(llm, cfg, registry, store, turn: dict, call: dict, hit: Hit, *,
              background: bool = False) -> Judgement:
    """The judgement on one call layer 1 picked (guard.Judgement). `background`: Watch's
    judge, after the call already ran."""
    her = await intent.read(store, turn, call)
    text = prompt(cfg, registry, turn, call, hit, her)
    took = time.monotonic()
    said, raw, model = await complete(
        llm, text + "\n\n" + prompts.questions()["answer"]["letter"],
        {"temperature": 0, "max_tokens": 1, "logprobs": True, "top_logprobs": 10},
        turn=turn, background=background)
    p = p_deny(raw, said)
    scores = {"p_deny": round(p, 4), "stages": 1,
              "ms1": round((time.monotonic() - took) * 1000)}
    if p < THRESHOLD:
        return Judgement(False, scores=scores, model=model)
    took2 = time.monotonic()
    said, _, model = await complete(
        llm, text + "\n\n" + prompts.questions()["answer"]["quote"],
        {"temperature": 0, "max_tokens": STAGE2_TOKENS,
         "response_format": {"type": "json_object"}}, turn=turn, background=background)
    v = parse_json(said)
    if not v and not background:
        raise ValueError("the judge's answer could not be read")   # On: the call fails
    scores.update(stages=2, ms2=round((time.monotonic() - took2) * 1000))
    quote = str(v.get("instruction_quote") or "").strip()
    why = " ".join(str(v.get("why") or "").split())[:300]
    if str(v.get("verdict") or "").strip().upper() != "D" or not quote:
        return Judgement(False, scores=scores, model=model)
    return Judgement(True, quote=quote, why=why, scores=scores, model=model)
