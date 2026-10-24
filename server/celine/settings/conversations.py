"""A conversation's own settings: its chat model (`conversations.model_id`), its
thinking level (`conversations.thinking`), its tool switches (`tool_overrides`) and
Bastet here or not (`conversations.guard_off`, guard/switch.py OFF).

Turns read model and thinking through the in-memory copies in
`settings.CONVERSATION_MODELS` / `CONVERSATION_THINKING`: filled once at startup,
updated by every write here. The database stays the source of truth; the copies only
save a read per model call.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import CONVERSATION_MODELS, CONVERSATION_THINKING
from .files import Invalid
from .thinking import OFF
from .thinking import valid as valid_thinking
from ..skills import gating


async def load_all(store) -> int:
    from ..guard.switch import OFF

    def fn(c: sqlite3.Connection) -> list[tuple[str, str | None, str | None]]:
        return [(r["id"], r["model_id"], r["thinking"]) for r in c.execute(
            "SELECT id, model_id, thinking FROM conversations "
            "WHERE model_id IS NOT NULL OR thinking IS NOT NULL")]
    rows = await store.read(fn)
    OFF.clear()
    OFF.update(await store.read(lambda c: [r[0] for r in c.execute(
        "SELECT id FROM conversations WHERE guard_off=1")]))
    CONVERSATION_MODELS.clear()
    CONVERSATION_THINKING.clear()
    for cid, model, thinking in rows:
        if model:
            CONVERSATION_MODELS[cid] = model
        if thinking:
            CONVERSATION_THINKING[cid] = thinking
    return len(rows)


async def get(store, cid: str) -> dict[str, Any] | None:
    """{model, thinking, tools, guard} as stored, or None when the conversation is gone."""
    def fn(c: sqlite3.Connection):
        row = c.execute("SELECT model_id, thinking, guard_off FROM conversations WHERE id=?",
                        (cid,)).fetchone()
        if row is None:
            return None
        tools = {r["target"]: bool(r["enabled"]) for r in c.execute(
            "SELECT target, enabled FROM tool_overrides WHERE conversation_id=?", (cid,))}
        return {"model": row["model_id"], "thinking": row["thinking"], "tools": tools,
                "guard": not row["guard_off"]}
    return await store.read(fn)


def validate(settings, registry, body: dict[str, Any]) -> dict[str, Any]:
    """Checks a (partial) settings object before anything is written. Keys: model
    ("provider/model" | null), thinking ("off", an effort word, or null), tools
    ({skill or skill__tool: true | false | null}), guard (true: Bastet here, false: she
    skips this chat; null: back to on). Returns the cleaned object."""
    out: dict[str, Any] = {}
    for k in body:
        if k not in ("model", "thinking", "tools", "guard"):
            raise Invalid(k, f"'{k}' is not a conversation setting (model, thinking, "
                             f"tools, guard).")
    if "guard" in body:
        if body["guard"] is not None and not isinstance(body["guard"], bool):
            raise Invalid("guard", "Bastet is true (here), false (off for this chat) or null.")
        out["guard"] = body["guard"] is not False
    if "model" in body:
        ref = body["model"]
        if ref is not None:
            if not isinstance(ref, str) or not ref:
                raise Invalid("model", "The model is \"provider/model\" or null.")
            m = settings.model(ref)
            if m is None:
                raise Invalid("model", f"There is no model '{ref}'. Add it to a provider "
                                       f"in Settings first.")
            if "chat" not in (m.get("caps") or []):
                raise Invalid("model", f"{m.get('label', ref)} cannot chat.")
        out["model"] = ref
    if "thinking" in body:
        t = body["thinking"]
        if t is not None and not valid_thinking(t):
            raise Invalid("thinking", f"Thinking is \"{OFF}\", one short effort word the "
                                      f"model accepts (such as high or xhigh), or null for "
                                      f"the model's default.")
        out["thinking"] = t
    if "tools" in body:
        tools = body["tools"]
        if not isinstance(tools, dict):
            raise Invalid("tools", "Tools are an object: {\"chat_archive\": false, "
                                   "\"chat_archive__search\": true}.")
        known = set(getattr(registry, "skills", {}) or {}) | set(
            getattr(registry, "tools", {}) or {})
        for t, v in tools.items():
            if registry is not None and t not in known:
                raise Invalid(f"tools.{t}", f"There is no skill or tool called '{t}'.")
            if v is not None and not isinstance(v, bool):
                raise Invalid(f"tools.{t}", "A tool is true (on), false (off) or null "
                                            "(back to the default).")
        out["tools"] = dict(tools)
    return out


async def apply(store, cid: str, clean: dict[str, Any]) -> bool:
    """Writes a validated (partial) settings object: merge-patch semantics, a key that
    is absent stays as it is, null resets it. Idempotent. False: no such conversation."""
    from ..conversations.store import set_settings
    if not await store.write(lambda c: set_settings(c, cid, clean)):
        return False
    if "guard" in clean:
        from ..guard.switch import OFF
        (OFF.discard if clean["guard"] else OFF.add)(cid)
    if "model" in clean:
        if clean["model"]:
            CONVERSATION_MODELS[cid] = clean["model"]
        else:
            CONVERSATION_MODELS.pop(cid, None)
    if "thinking" in clean:
        if clean["thinking"]:
            CONVERSATION_THINKING[cid] = clean["thinking"]
        else:
            CONVERSATION_THINKING.pop(cid, None)
    # skills first: a skill's switch clears the tool rows under it
    for target, on in sorted((clean.get("tools") or {}).items(), key=lambda kv: "__" in kv[0]):
        if on is None:
            await gating.clear_override(store, cid, target)
            continue

        def seq(c: sqlite3.Connection, target=target) -> int:
            r = c.execute("SELECT client_seq FROM tool_overrides WHERE conversation_id=? "
                          "AND target=?", (cid, target)).fetchone()
            return int(r["client_seq"]) if r else 0
        # the server's own write: always newer than what a client sent before
        await gating.set_override(store, cid, target, bool(on), await store.read(seq) + 1)
    return True


def _model(s, ref: str | None) -> dict[str, Any]:
    """{model, label, provider_label} of a model ref; all None without one."""
    pid = s.split(ref)[0] if ref else None
    return {"model": ref, "label": (s.model(ref) or {}).get("label", ref) if ref else None,
            "provider_label": (s.providers.get(pid) or {}).get("label", pid) if pid else None}


def view(s, cid: str, stored: dict) -> dict[str, Any]:
    """What GET /conversations/{cid}/settings answers. `stored`: get's. `effective`:
    the model in use and `default`, what Default comes to now (None: no model)."""
    chain = s.chat_chain(cid)
    default = s.default_chain(cid)
    picked = stored.get("model")
    from .features import effective
    on = effective(getattr(s, "features", None))["guard"]
    return {"model": picked if picked and s.model(picked) is not None else None,
            "thinking": stored.get("thinking"),
            "tools": stored.get("tools") or {},
            "guard": stored.get("guard", True),
            "effective": {
                **_model(s, chain[0] if chain else None),
                "default": _model(s, default[0]) if default else None,
                "chain": chain,
                # Bastet's feature switch: off there, this chat's switch does nothing
                "guard": on}}
