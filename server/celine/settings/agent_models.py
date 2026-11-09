"""The models it may use itself: in its model tool (skills/models.py), as a
sub-agent's model (sub_agents/skill.py) and through the model proxy its shellbox's
agent CLIs call (api/llm_proxy.py).

There is no list of its own: every model in the saved providers that can chat and
whose provider has its key is its own, in the providers' order.

A cloud model (`cloud`: its provider's Cloud switch, else its address) it picked
itself waits for the user's approval at each use (`ask_cloud`): a sub-agent's start or
resume, models__ask, code__start, missions__start and a plan step's model. A model the
user picked never asks (`picked_by_user`): one in any role's list (unless Ask before
cloud models from your lists is on, general `ask_listed_cloud`), the conversation's own
model, or one they set on a step of its plan. A code__start on a model the box is
served already (`granted`) asks nothing: that approval stands until its grant ends.
"""
from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from ..core.ids import iso
from . import general
from .kinds import KINDS, caps_of

LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal")
BOX_GRANT = 600     # s a cloud model approved for code__start stays served to the box


def allowed(s) -> list[str]:
    """Every model in the providers that can chat (settings/kinds.py caps_of) and has
    its key, in their order."""
    return [f"{pid}/{name}" for pid, p in s.providers.items()
            if not s.key_missing(pid)
            for name in (p.get("models") or {}) if "chat" in caps_of(p, name)]


def label(s, ref: str) -> str:
    """A model as it reads it: its label and its provider's."""
    m = s.model(ref) or {}
    p = s.providers.get(s.split(ref)[0]) or {}
    return f"{m.get('label') or s.split(ref)[1]} on {p.get('label') or s.split(ref)[0]}"


def cloud(s, ref: str) -> bool:
    """A model whose provider is someone else's cloud (provider_cloud)."""
    return provider_cloud(s.providers.get(s.split(ref)[0]) or {})


def provider_cloud(p: dict) -> bool:
    """The provider's Cloud switch when it has one, else whether its address (its
    base_url, else its kind's `default_url`) is outside this network. Local: every
    address that is not global (loopback, private, link-local, ULA, Tailscale and
    CGNAT), a single-label host, and the .local, .lan, .home.arpa and .internal names."""
    if isinstance(p.get("cloud"), bool):
        return p["cloud"]
    url = p.get("base_url") or KINDS.get(p.get("kind"), {}).get("default_url") or ""
    return not local_host(urlsplit(url if "://" in url else f"http://{url}").hostname or "")


def local_host(host: str) -> bool:
    """A host name or address on this network (provider_cloud)."""
    host = host.rstrip(".").lower()
    if not host or host == "localhost" or "." not in host and ":" not in host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host.endswith(LOCAL_SUFFIXES)
    return not ip.is_global


def picked_by_user(s, ref: str, conversation_model: str | None,
                   plan: dict | None = None) -> bool:
    """A model the user picked: one in any role's list (a fallback reaches it as
    listed) while Ask before cloud models from your lists is off, the conversation's own
    model (`conversation_model`), or one the user set on a step of its plan (`plan`,
    plans/store.py: the step's `by`)."""
    listed = not general.value(s.general, "ask_listed_cloud") and any(
        ref in (r.get("chain") or []) for r in s.roles.values())
    return ref == conversation_model or listed or any(
        st["model"] == ref and st["by"] == "user" for st in (plan or {}).get("steps") or ())


# A cloud model approved for code__start (ask_cloud box=True) is served to the
# shellbox's model proxy (api/llm_proxy.py yours) until BOX_GRANT s after the approval
# or its last call there: a row of `model_grants`, so an app restart keeps it for the
# session still working in the box.

def _now() -> datetime:         # tests: a fixed clock
    return datetime.now(timezone.utc)


def grant_sync(c, ref: str) -> None:
    """Inside a write: `ref` is served to the box for BOX_GRANT s from now."""
    c.execute("INSERT INTO model_grants(ref, until) VALUES(?, ?) ON CONFLICT(ref) DO "
              "UPDATE SET until=excluded.until",
              (ref, iso(_now() + timedelta(seconds=BOX_GRANT))))


def keep_sync(c, ref: str) -> None:
    """Inside a write: a call on `ref` keeps its live grant, when it has one, for
    BOX_GRANT s from now."""
    t = _now()
    c.execute("UPDATE model_grants SET until=? WHERE ref=? AND until>?",
              (iso(t + timedelta(seconds=BOX_GRANT)), ref, iso(t)))


def granted(c) -> set[str]:
    """Inside a read: the models served to the box now."""
    return {r[0] for r in c.execute("SELECT ref FROM model_grants WHERE until>?",
                                    (iso(_now()),))}


async def ask_cloud(cfg, app, ctx, ref: str | None, *, action: str,
                    facts: list[tuple[str, str]] | tuple = (),
                    detail: tuple[str, str] | None = None, box: bool = False,
                    plan: dict | None = None) -> str | None:
    """The user's approval before work of tool call `ctx` goes to `ref`, a cloud model
    the user did not pick (picked_by_user: the call's conversation's own model, and
    `plan`, its plan as plan__set or plan__step leaves it). None when it may go, else
    the sentence for the tool's result: its provider is known down (nothing to ask
    about), nobody can approve, or approvals.ask's answer. The card reads "{who} asks
    to {action}.", `facts` first, then Model and Provider. `box`: the approved model is
    served to the shellbox's model proxy (grant_sync), and a model it serves already
    asks nothing."""
    s = cfg.settings
    if not ref or ref not in allowed(s) or not cloud(s, ref) or \
            picked_by_user(s, ref, s.conversation_models.get(ctx.conversation_id), plan):
        return None             # none of its models (the tool refuses it), or no ask
    llm = getattr(app, "llm", None)
    if llm is not None:             # known down: nothing to ask the user about
        st = llm.router.provider_state(s.split(ref)[0])
        if st.reachable is False:
            return (f"{label(s, ref)} does not answer; nothing was done."
                    + (f" {st.error}" if st.error else ""))
    if box and ref in await ctx.store.read(granted):
        return None             # approved for the box already: the grant still serves it
    approvals = getattr(app, "approvals", None)
    if approvals is None:
        return "Nobody can approve a cloud model here; nothing was done."
    pid, mid = s.split(ref)
    no = await approvals.ask(
        "model", ctx.conversation_id, action,
        facts=[*facts, ("Model", (s.model(ref) or {}).get("label") or mid),
               ("Provider", (s.providers.get(pid) or {}).get("label") or pid)],
        detail=detail, call=ctx.tool_call_id or None)
    if no is None and box:
        await ctx.store.write(lambda c: grant_sync(c, ref))
    return no
