"""The escalation chain: who looks at a finding, in order.

- L0 code (facts.py): no fact, nothing asked.
- L1 the small role (questions.SmallRole, the hijack judge): JSON {verdict, quote, why},
  a yes held to a quote. A no ends it.
- L2 a second look by a big reasoning model, only on an L1 yes that stopped, froze or
  would have (flow), and on a patrol finding before the owner hears of it. guard.toml
  [chain] names it: `provider` (a providers.toml provider) and `model`; empty provider:
  no L2. Asked directly (OpenAI chat API) with the header X-Priority: background, so the
  provider's swapper loads it only once its own model is idle; `seconds` the most it
  may take, swap included; `thinking` "off" (the default) or a level, sent the way a
  chat sends it to that provider's kind (settings/thinking.apply).

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
from types import SimpleNamespace

import httpx

from ..settings.registry import provider_key
from ..settings.thinking import OFF, apply
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
    """{url, model, key, seconds, kind, thinking} of L2, None when [chain] names no provider or the
    provider does not exist."""
    ch = rule_set.current(cfg)["chain"]
    pid, model = ch["provider"].strip(), ch["model"].strip()
    s = getattr(cfg, "settings", None)
    p = (getattr(s, "providers", None) or {}).get(pid) if pid and model else None
    if not p or not str(p.get("base_url") or "").strip():
        return None
    base = str(p["base_url"]).strip().rstrip("/")
    url = (base if base.endswith("/v1") else base + "/v1") + "/chat/completions"
    return {"url": url, "model": model, "seconds": float(ch["seconds"]),
            "kind": p.get("kind", "openai"), "thinking": ch["thinking"],
            "key": provider_key(pid, getattr(s, "secrets_root", None))}


def _thinking(l2: dict) -> dict:
    """The request fields for L2's thinking, as a chat sends them (thinking.apply, then
    the backend: params into the body, a llama-server's `llama` block over it)."""
    t = l2.get("thinking") or OFF
    bc = SimpleNamespace(raw={"provider_kind": l2.get("kind", "openai")}, params={
        "max_tokens": TOKENS if t == OFF else THINKING_TOKENS})
    out = apply(bc, t)
    return {**out.params, **(out.raw.get("llama") or {})}


def _answer(message: dict) -> dict | None:
    """L2's JSON answer: in content, else in reasoning_content (a template or parser
    that put the answer there); None when neither holds one with a Y or N verdict."""
    for key in ("content", "reasoning_content", "reasoning"):
        v = parse_json(str(message.get(key) or ""))
        if str(v.get("verdict") or "").strip().upper()[:1] in ("Y", "N"):
            return v
    return None


async def ask(l2: dict, text: str, transport: httpx.AsyncBaseTransport | None = None
              ) -> dict:
    """One L2 answer: {verdict, quote, why, model, ms}. Raises Unavailable when it cannot
    answer in time."""
    body = {"model": l2["model"], "messages": [{"role": "user", "content": text}],
            # a judge: low temperature; no response_format: tabbyAPI's JSON mode
            # can mangle the keys ("/verdict/")
            "temperature": 0.2, "top_p": 0.95, "top_k": 20,
            **_thinking(l2)}
    headers = {"X-Priority": "background"}
    if l2.get("key"):
        headers["Authorization"] = f"Bearer {l2['key']}"
    took = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=l2["seconds"], transport=transport) as c:
            r = await c.post(l2["url"], json=body, headers=headers)
    except httpx.TimeoutException:
        raise Unavailable(f"{l2['model']} took longer than {l2['seconds']:.0f} s") from None
    except httpx.HTTPError as e:
        raise Unavailable(f"{l2['model']} at {l2['url']} is not reachable "
                          f"({type(e).__name__})") from None
    if r.status_code >= 400:
        raise Unavailable(f"{l2['model']} at {l2['url']} answered {r.status_code}")
    try:
        v = _answer(r.json()["choices"][0]["message"])
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        v = None
    if v is None:
        raise Unavailable(f"{l2['model']} gave no verdict")
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
        her = intent.build(c, {"id": row["turn_id"], "conversation_id": row["conversation_id"]},
                           {"message_id": row["message_id"]})
    except KeyError:
        her = "(none)"
    text = cfg.substitute(prompts.text("review"), braces=True).format(
        intent=caps.tail(her, caps.INTENT), action=action, fact=row["piece"] or "(none)",
        quote=row["quote"] or "(none)", why=row["why"] or "(none)", question=question)
    return text, seen


async def review(app, rid: int, *, transport=None) -> str:
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
        got = await ask(l2, text, transport)
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
    why = (await _last(app, rid)).get("why") or "no answer"
    return bastet.fill(app.cfg, bastet.SECOND_LOOK_AWAY, why=why)


async def after_stop(app, rid: int, note: str, conversation_id: str, turn_id: str,
                     *, transport=None) -> str:
    """An L1 stop or freeze in its turn, looked at again in the background: the owner's
    note goes out unless L2 says no, with why when L2 did not answer; a freeze L2 says no
    to is lifted, with its why."""
    said = await review(app, rid, transport=transport)
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
