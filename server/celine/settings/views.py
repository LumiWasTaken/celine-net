"""What the Settings pages show: one view per section, built from the loaded settings
and the app's services. The routes (api/settings.py) only call these and answer."""
from __future__ import annotations

import copy
import logging
from typing import Any

from . import features as features_mod, kinds, mail as mail_mod, secrets as secrets_mod
from . import services as services_mod, tools_meta, vault as vault_mod
from .files import version
from .registry import ROLES, key_state

log = logging.getLogger("celine.settings")


def model_router(services):
    """The router the app's models go through (a test's app may have no llm)."""
    from ..backends.router import app_router
    return services.llm.router if services.llm is not None else app_router(services.cfg)


# ---- providers -----------------------------------------------------------------------

def used_by(s, pid: str, model: str | None = None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for role, r in s.roles.items():
        for ref in r.get("chain") or []:
            p, _, m = ref.partition("/")
            if p == pid and (model is None or m == model):
                out.setdefault(m, []).append(role)
    return out


def _task_labels(roles) -> list[str]:
    """The task names the Models page shows, in its order."""
    return [spec["label"] for r, spec in ROLES.items() if r in roles]


def provider(s, pid: str, router) -> dict[str, Any]:
    from .thinking import levels
    p = s.providers[pid]
    used = used_by(s, pid)
    missing = s.key_missing(pid)
    st = router.provider_state(pid).public()
    models = []
    for name, m in (p.get("models") or {}).items():
        ref = f"{pid}/{name}"
        ms = router.model_state(ref)
        models.append({"name": name, "ref": ref, **m,
                       # the context the server says it runs with: turns budget with
                       # it over `context` (turns/tokens.py); null when no server says
                       "server_context": router.server_context(pid, name, m.get("context")),
                       "thinking_offered": levels(p, m),
                       "used_by": _task_labels(used.get(name, [])),
                       # a provider without its key offers nothing to choose from
                       "offered": not missing,
                       "state": ms.public() if ms is not None else {"state": "ok"}})
    return {"id": pid, "provider": p, "version": version(p),
            "key": key_state(pid, p, s.secrets_root),
            "state": {**st, "key_missing": missing},
            "models": models,
            "used_by": _task_labels({r for rs in used.values() for r in rs})}


def kinds_view() -> list[dict[str, Any]]:
    from .registry import OPTIONS
    return [{"kind": k, "label": v["label"], "key": v["key"],
             "default_url": v.get("default_url"),
             "options": [{"key": o, "type": t, "default": d, "choices": list(c) if c else None}
                         for o, (t, d, c) in OPTIONS[k].items()]}
            for k, v in kinds.KINDS.items()]


# the roles an empty one of fills itself from a new provider's models: the first model
# with the role's caps, every preset for the ComfyUI roles. Settings shows it, change it there.
AUTO_ROLES = ("chat", "small", "embeddings", "reranker", "vision",
              "image_generation", "music_generation", "transcription", "speech")
_ALL = ("image_generation", "music_generation")


def fill_roles(s, pid: str, provider: dict[str, Any]) -> dict[str, Any] | None:
    """The roles with the empty ones filled from `provider`'s models, or None when no
    role changes."""
    roles = copy.deepcopy(s.roles)
    changed = False
    for role in AUTO_ROLES:
        if s.chain(role):
            continue
        need = set(ROLES[role]["caps"])
        fits = [f"{pid}/{n}" for n, m in (provider.get("models") or {}).items()
                if need <= set(m.get("caps") or [])]
        if fits:
            roles[role] = {**roles.get(role, {}), "chain": fits[:6] if role in _ALL else fits[:1]}
            changed = True
    return roles if changed else None


async def discovered(s, pid: str, router) -> list[dict] | None:
    """What a provider's server reports about its models now, for "Check again"; None
    when there is nothing to merge (ComfyUI, no saved models) or the server did not
    answer (its state, from the probe, says why)."""
    from .registry import provider_key
    p = s.providers.get(pid)
    if not p or p.get("kind") == "comfyui" or not p.get("models"):
        return None
    try:
        return await kinds.discover(p["kind"], p["base_url"], provider_key(pid, s.secrets_root),
                                    refresh=True, transport=getattr(router, "transport", None))
    except Exception as e:
        log.warning("model discovery failed", extra={"event": "settings.discover_failed",
                                                      "provider": pid, "error": repr(e)})
        return None


def merge_discovered(s, pid: str, found: list[dict]) -> dict[str, Any] | None:
    """The providers with `pid`'s saved models picking up the capabilities, context and
    thinking levels its server now reports, user edits kept; None when nothing changes."""
    by_id = {m["id"]: m for m in found if isinstance(m, dict) and m.get("id")}
    p = s.providers.get(pid)
    if not by_id or not p or not p.get("models"):
        return None
    providers = copy.deepcopy(s.providers)
    changed = False
    for mid, stored in providers[pid]["models"].items():
        d = by_id.get(mid)
        if not d:
            continue
        caps = list(dict.fromkeys([*(stored.get("caps") or []), *(d.get("caps") or [])]))
        if caps and caps != stored.get("caps"):
            stored["caps"] = caps
            changed = True
        if not stored.get("context") and isinstance(d.get("context"), int):
            stored["context"] = d["context"]
            changed = True
        if not stored.get("thinking_levels") and d.get("thinking_levels"):
            stored["thinking_levels"] = list(d["thinking_levels"])
            changed = True
    return providers if changed else None


# ---- roles ---------------------------------------------------------------------------

def roles(s) -> dict[str, Any]:
    out = {}
    for role, spec in ROLES.items():
        r = s.roles.get(role) or {"chain": []}
        chain = []
        for ref in r.get("chain") or []:
            m = s.model(ref) or {}
            pid = s.split(ref)[0]
            chain.append({"ref": ref, "label": m.get("label", ref),
                          "provider": pid,
                          "provider_label": (s.providers.get(pid) or {}).get("label", pid),
                          "key_missing": s.key_missing(pid)})
        out[role] = {"label": spec["label"],
                     "needs": spec["caps"], "chain": r.get("chain") or [],
                     "models": chain, "version": version(s.roles.get(role))}
    return out


# ---- tools ---------------------------------------------------------------------------

def place_labels(cfg) -> dict[str, str]:
    """Where a turn runs (skills/places.py), in the words the Tools page uses."""
    return {k: cfg.substitute(v) for k, v in tools_meta.PLACE_LABELS.items()}


def tools(services) -> dict[str, Any]:
    from ..skills.catalog import status_of
    from ..skills.gating import default_enabled
    from ..skills.places import PLACE_NAMES, offered
    cfg = services.cfg
    s = cfg.settings
    reg = services.registry
    skills = []
    if reg is not None:
        for name, skill in reg.skills.items():
            specs = [t for t in reg.tools.values() if t.skill == name]
            meta = tools_meta.skill_meta(name, getattr(skill, "description", ""),
                                         cfg.substitute)
            rows = []
            for t in specs:
                tm = tools_meta.tool_meta(t.wire_name, t.name, t.description, cfg.substitute)
                on = default_enabled(t, cfg)
                rows.append({"wire_name": t.wire_name, "name": t.name, **tm,
                             "mutating": t.mutating, "default": "on" if on else "off",
                             "set_in_settings": t.wire_name in s.tools})
            rows.sort(key=lambda x: x["label"].lower())
            states = {x["default"] for x in rows}
            down = getattr(skill, "search_down", None)     # web: SearXNG does not answer
            skills.append({"name": name, **meta,
                           "source": "mcp" if name in (cfg.mcp or {}) else "built-in",
                           "status": "down" if down else status_of(services, name, skill),
                           "error": down or getattr(skill, "error", None),
                           "default": states.pop() if len(states) == 1 else "mixed",
                           "set_in_settings": name in s.tools,
                           "places": [p for p in PLACE_NAMES if offered(name, p)],
                           "tools": rows})
    skills.sort(key=lambda x: (x["source"] != "built-in", x["label"].lower()))
    labels = place_labels(cfg)
    return {"skills": skills, "places": [{"name": p, "label": labels[p]} for p in PLACE_NAMES],
            "defaults": dict(s.tools), "version": version(s.tools),
            "saved": bool(s.saved.get("tools"))}


# ---- features and services -----------------------------------------------------------

async def features(services, probes: dict | None = None) -> dict[str, Any]:
    """`probes`: the states to show (a PUT answers with the ones the page saw: no probe,
    with its DNS wait, on a save); None asks probe_all (cached 10 s)."""
    s = services.cfg.settings
    if probes is None:
        probes = await features_mod.probe_all(services.cfg, services)
    return {"features": features_mod.effective(s.features),
            "catalog": [{**features_mod.view(f), **probes[f.id],
                         "card": features_mod.card_now(services.cfg, f, probes[f.id])}
                        for f in features_mod.CATALOG],
            "saved": bool(s.saved.get("features")), "done": features_mod.done(s.features),
            "version": version(s.features)}


async def apply_runners(services, changed: set[str] = frozenset()) -> None:
    """What runs for a feature starts or stops at once (core/runners.py)."""
    if services.runners is not None:         # a test's app has none
        await services.runners.apply(features_mod.effective(services.cfg.settings.features),
                                     changed)
    services.feature_probes = None


def services_view(services) -> dict[str, Any]:
    """The addresses of the shellbox, the browser and SearXNG, and the hash of each box's
    key (settings/services.py, celine/keys.py): what goes into a box's key.hash file."""
    from .. import keys
    s = services.cfg.settings
    return {"fields": services_mod.FIELDS,
            "services": {sid: {f["key"]: services_mod.value(s.services, sid, f["key"])
                               for f in services_mod.FIELDS[sid]} for sid in services_mod.IDS},
            "keys": {sid: {"hash": keys.key_hash(services.cfg.data_dir, name)}
                     for sid, name in services_mod.KEYS.items()},
            "version": version(s.services), "saved": bool(s.saved.get("services"))}


# ---- email, vault -------------------------------------------------

def email(cfg) -> dict[str, Any]:
    s = cfg.settings
    root = secrets_mod.secrets_dir(cfg.data_dir)
    st = secrets_mod.state(root, mail_mod.SECRET)
    return {"fields": mail_mod.FIELDS,
            "email": {f["key"]: mail_mod.value(s.email, f["key"]) for f in mail_mod.FIELDS},
            "password": {"state": st["state"], "set_at": st["set_at"]},
            "version": version(s.email), "saved": bool(s.saved.get("email"))}


def vault(services) -> dict[str, Any]:
    s = services.cfg.settings
    root = secrets_mod.secrets_dir(services.cfg.data_dir)
    v = services.vault
    return {"fields": vault_mod.FIELDS,
            "vault": {f["key"]: vault_mod.value(s.vault, f["key"]) for f in vault_mod.FIELDS},
            "client_secret": secrets_mod.state(root, vault_mod.CLIENT_SECRET),
            "unlocked": v.session is not None,
            "asks": services.approvals.view("vault") if services.approvals is not None else [],
            "status": v.status,
            "version": version(s.vault), "saved": bool(s.saved.get("vault"))}
