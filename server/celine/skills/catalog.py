"""The skills catalog the Tools pages show: per skill its state, per tool its label,
summary and switches."""
from __future__ import annotations

from ..settings import tools_meta
from .gating import default_enabled, is_enabled, reason


# a skill's state as every place says it (Settings, Tools; a chat's panel), in the words
# the sources use; a state without a word works
WORDS = {"down": "Not connected", "dead": "Not connected", "unconnected": "Not connected",
         "unset": "Not set up"}


def state_of(services, name: str, skill) -> dict[str, str | None]:
    """{status, word, error}: status_of, but a skill whose feature's last probe blocks its
    switch (Settings, Features greys it) is unset or down with the probe's sentence, and
    web whose last search failed is down with why."""
    from ..settings import features
    hit = getattr(services, "feature_probes", None)
    probe = hit[1].get(features.skill_owners().get(name, "")) if hit else None
    down = getattr(skill, "search_down", None)
    if probe is not None and features.shown(probe)["blocks"]:
        status = "unset" if probe["state"] == "unset" else "down"
        error = probe.get("detail") or None
    elif down:
        status, error = "down", down
    else:
        status, error = status_of(services, name, skill), getattr(skill, "error", None)
    return {"status": status, "word": WORDS.get(status), "error": error}


def status_of(services, name: str, skill) -> str:
    """The skill's state; a source's server (Discord, WhatsApp) that answers while its
    account is not connected is `unconnected`: its tools have nothing to work on."""
    status = getattr(skill, "status", "up")
    scfg = (services.cfg.mcp or {}).get(name)
    connectors = services.connectors
    c = connectors.get(scfg.kb) if status == "up" and scfg is not None and scfg.kb \
        and connectors is not None else None
    if c is not None and c.connection().get("state") != "connected":
        return "unconnected"
    return status


def catalog(services, over: dict, here: set | None) -> list[dict]:
    """Every skill with its tools; `here`: only these tools (a conversation's kind),
    `over`: its switches."""
    reg, cfg = services.registry, services.cfg
    out = []
    for name, skill in reg.skills.items():
        tools = [t for t in reg.tools.values() if t.skill == name
                 and (here is None or t.wire_name in here)]
        if here is not None and not tools:
            continue
        meta = tools_meta.skill_meta(name, skill.description, cfg.substitute)
        rows = []
        for t in tools:
            tm = tools_meta.tool_meta(t.wire_name, t.name, t.description, cfg.substitute)
            rows.append({"name": t.name, "wire_name": t.wire_name, **tm,
                         "description": tools_meta.first_sentence(
                             cfg.substitute(t.description)),
                         "mutating": t.mutating,
                         "enabled": is_enabled(t, over, cfg),
                         "default": default_enabled(t, cfg),
                         "why": reason(t, over, cfg)})
        rows.sort(key=lambda x: x["label"].lower())
        out.append({
            "name": name, **meta,
            "description": cfg.substitute(skill.description),
            "enabled_globally": cfg.skill_enabled(name),
            **state_of(services, name, skill),
            "tools": rows,
        })
    return out
