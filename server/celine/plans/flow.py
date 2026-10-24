"""What a turn reads of its plan: the block in its context each turn, whether a turn may
end, and the model the step it is on runs on (a long task can switch model halfway)."""
from __future__ import annotations

from . import store as plans
from ..core.event_types import CONVERSATION_SETTINGS


async def current(store, cid: str) -> dict | None:
    """The plan of conversation `cid`, None when it has none."""
    return await plans.get(store, cid)


def stops(plan: dict | None, calls: list[dict]) -> bool:
    """A plan__step of this step reached an ask checkpoint: the turn ends there."""
    return bool(plan) and plan["waiting"] and any(c["name"] == "plan__step" for c in calls)


def keeps_going(plan: dict | None) -> bool:
    """A step is open and it is not waiting on the user: a quiet step does not end the
    turn (turns/runner.py), a mission gets its next turn (missions/watch.py)."""
    return plans.is_open(plan) and not plan["waiting"]


def block(plan: dict | None) -> str | None:
    """The plan as it reads it, {{username}} unsubstituted; None when it is empty."""
    if not plan or not (plan["steps"] or plan["text"].strip()):
        return None
    mark = {"done": "[x]", "doing": "[>]", "todo": "[ ]"}
    lines = ["Your plan here (plan__step moves a step, plan__set rewrites it):"]
    if plan["text"].strip():
        lines.append(plan["text"].strip())
    for n, s in enumerate(plan["steps"], 1):
        extra = [x for x in ("ask {{username}} when done" if s["checkpoint"] == "ask"
                             else "", f"model {s['model']}" if s["model"] else "") if x]
        lines.append(f"{mark[s['state']]} {n}. {s['title']}" +
                     (f" ({', '.join(extra)})" if extra else ""))
    if plan["waiting"]:
        lines.append("You asked {{username}} at a checkpoint: go on when they answer.")
    elif plans.is_open(plan):
        lines.append("Keep going until every step is done. Stop at a step marked ask, "
                     "or when only {{username}} can decide.")
    else:
        lines.append("Every step is done.")
    return "\n".join(lines)


def chat_models(settings) -> list[tuple[str, str]]:
    """(ref, label) of every model in Settings that can chat and has its key."""
    out = []
    for pid, p in (getattr(settings, "providers", None) or {}).items():
        if settings.key_missing(pid):
            continue
        for mid, m in (p.get("models") or {}).items():
            if "chat" in (m.get("caps") or []):
                out.append((f"{pid}/{mid}", str(m.get("label") or mid)))
    return out


def check_models(settings, steps: list[dict]) -> None:
    """Every model a step names is a chat model in Settings. Refused otherwise."""
    known = dict(chat_models(settings)) if settings is not None else {}
    for n, s in enumerate(steps, 1):
        if s["model"] and s["model"] not in known:
            raise plans.Refused(f"Step {n}: there is no chat model {s['model']!r}. The "
                                f"models are: {', '.join(known) or 'none'}.")


async def follow_model(store, cfg, cid: str, plan: dict) -> str | None:
    """The step it is on names a model: the conversation runs on it from its next turn
    (its `conversation.settings` event). -> the model switched to, None."""
    from ..settings import conversations as conv
    n = plans.doing(plan)
    model = plan["steps"][n - 1]["model"] if n else None
    settings = getattr(cfg, "settings", None)
    if not model or settings is None or (await conv.get(store, cid) or {}).get("model") == model:
        return None
    await conv.apply(store, cid, {"model": model})
    await store.append_event(cid, CONVERSATION_SETTINGS,
                             conv.view(settings, cid, await conv.get(store, cid)))
    return model
