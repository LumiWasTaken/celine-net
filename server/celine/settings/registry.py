"""Providers, models and model roles: the shapes and their validation.

- A **provider** is one server (llama-swap, llama-server, Ollama, OpenRouter, Anthropic,
  any OpenAI-compatible server, ComfyUI): kind, address, options, and the models chosen
  from it. A ComfyUI provider's models are its presets, images and music
  (comfyui/presets.py). Its key lives in
  var/secrets/<provider>.key (write-only in the UI).
- A **model ref** `"<provider>/<model>"` (split on the first `/`) is the unit every
  role and conversation picks.
- A **role** (roles.toml) is what the app uses a model for: chat, titles and
  descriptions, tasks, its words in its own voice (writing: skills/writer.py), browser,
  semantic topic search (Topic search), reranking, photo descriptions, image generation
  and music generation (lists of presets). A chat runs on the chat role, every other
  kind on tasks (settings/roles.py).
  Each has an ordered list of models: a turn the user waits on runs on the first
  only, background work on the first that can answer (backends/llm.py). Any
  provider can serve any role; where the work runs follows only from these lists.

Consumers read `cfg.settings` (this module's `Settings`) and ask the router
(backends/router.py) which model to use; nothing is compiled into other shapes.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import BackendConfig, Problem
from . import kinds as kinds_mod
from .files import Invalid

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MODEL = re.compile(r"^[^\s\"'\\]{1,200}$")
TARGET = re.compile(r"^[A-Za-z0-9_-]+(__[A-Za-z0-9_.-]+)?$")
# an OpenRouter provider slug: "deepinfra", "deepinfra/fp4"
PROVIDER_SLUG = re.compile(r"^[a-z0-9._-]+(/[a-z0-9._-]+)?$")

# role -> label and the capabilities a model needs for it
ROLES: dict[str, dict[str, Any]] = {
    "chat": {"label": "Chat", "caps": ["chat"]},
    "tasks": {"label": "Tasks", "caps": ["chat", "tools"]},
    "writer": {"label": "Writing", "caps": ["chat"]},
    "small": {"label": "Titles and descriptions", "caps": ["chat"]},
    "embeddings": {"label": "Topic search", "caps": ["embeddings"]},
    "reranker": {"label": "Reranking", "caps": ["rerank"]},
    "vision": {"label": "Photo descriptions", "caps": ["vision"]},
    "screen": {"label": "Screen reading", "caps": ["vision"]},
    "image_generation": {"label": "Image generation", "caps": ["image_generation"]},
    "music_generation": {"label": "Music generation", "caps": ["music_generation"]},
    "transcription": {"label": "Dictation", "caps": ["transcription"]},
    "speech": {"label": "Read aloud", "caps": ["speech"]},
}

# option name -> (type, default, choices) per kind; types: bool int num str list
_COMMON = {"timeout": ("num", 600.0, None), "max_concurrent": ("int", 0, None),
           # effort words its models accept (the UI's list); a model can say its own
           "thinking_levels": ("list", [], None)}
OPTIONS: dict[str, dict[str, tuple]] = {
    "llama-swap": dict(_COMMON),
    "tabby": dict(_COMMON),
    "llama-server": dict(_COMMON),
    "ollama": {**_COMMON, "keep_alive": ("str", "", None)},
    "openai": dict(_COMMON),
    "anthropic": dict(_COMMON),
    # timeout: the most one item of a batch may take in ComfyUI; shares_gpu_with: the
    # id of a llama-swap provider on the same GPU (backends/llm.py gpu_lease)
    "comfyui": {"timeout": ("num", 600.0, None), "shares_gpu_with": ("str", "", None)},
    "openrouter": {
        **_COMMON,
        "service_tier": ("str", "", ("", "flex", "priority")),
        "data_collection": ("str", "deny", ("allow", "deny")),
        "order": ("list", [], None),
        "only": ("list", [], None),
        "ignore": ("list", [], None),
        "allow_fallbacks": ("bool", True, None),
        "sort": ("str", "", ("", "price", "throughput", "latency")),
        "require_parameters": ("bool", True, None),
        "quantizations": ("list", [], None),
        "backup_models": ("list", [], None),
    },
}
_PROVIDER_KEYS = {"kind", "label", "base_url", "options", "models"}
_MODEL_KEYS = {"label", "caps", "context", "slots", "params", "llama", "reasoning_effort",
               "thinking_levels", "zdr", "provider", "vision_twin", "max_tokens", "cache_ttl"}
# an Anthropic model's `max_tokens` (the API needs one) when unset; its most is what the
# provider states (settings/kinds.py server_output)
MAX_TOKENS = 8192
CACHE_TTLS = ("5m", "1h")       # an Anthropic model's prompt cache time; 5m when unset


# ---- the runtime object ------------------------------------------------------------

@dataclass
class Settings:
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    roles: dict[str, dict[str, Any]] = field(default_factory=dict)
    tools: dict[str, str] = field(default_factory=dict)           # target -> on|off
    general: dict[str, Any] = field(default_factory=dict)
    email: dict[str, Any] = field(default_factory=dict)       # settings/mail.py
    vault: dict[str, Any] = field(default_factory=dict)       # settings/vault.py
    triggers: dict[str, Any] = field(default_factory=dict)    # celine/triggers
    guard: dict[str, Any] = field(default_factory=dict)       # guard/rules.py
    features: dict[str, bool] = field(default_factory=dict)   # settings/features.py
    services: dict[str, dict[str, Any]] = field(default_factory=dict)  # settings/services.py
    address: dict[str, str] = field(default_factory=dict)     # settings/address.py
    plugins: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> its form's values
    saved: dict[str, bool] = field(default_factory=dict)   # section -> var/settings file
    problems: list[Problem] = field(default_factory=list)
    secrets_root: Any = None
    conversation_models: dict[str, str] = field(default_factory=dict)  # cid -> ref
    conversation_thinking: dict[str, str] = field(default_factory=dict)  # cid -> level
    conversation_roles: dict[str, str] = field(default_factory=dict)  # cid -> role

    # -- lookups
    def value(self, key: str) -> Any:
        """A general or Advanced setting as saved, else its default (settings/general.py)."""
        from .general import value
        return value(self.general, key)

    def typed_keys(self) -> list[str]:
        """The keys typed into Settings (var/secrets), for redaction (Config.secrets)."""
        from .secrets import all_keys
        return all_keys(self.secrets_root) if self.secrets_root is not None else []

    @property
    def skills_off(self) -> frozenset[str]:
        """The skills whose feature is off (settings/features.py, config.skill_enabled)."""
        from .features import skills_off
        return skills_off(self.features)

    def split(self, ref: str) -> tuple[str, str]:
        prov, _, model = str(ref).partition("/")
        return prov, model

    def model(self, ref: str) -> dict[str, Any] | None:
        prov, model = self.split(ref)
        p = self.providers.get(prov)
        return None if p is None else (p.get("models") or {}).get(model)


    def key_missing(self, provider_id: str) -> bool:
        """The provider needs a key and none is saved: its models are not offered."""
        p = self.providers.get(provider_id)
        if p is None or kinds_mod.KINDS[p["kind"]]["key"] != "required":
            return False
        return not provider_key(provider_id, self.secrets_root)

    def chain(self, role: str) -> list[str]:
        return list((self.roles.get(role) or {}).get("chain") or [])

    def chat_chain(self, conversation_id: str | None = None) -> list[str]:
        """The conversation's model first (if it picked one), then the list of its
        role (settings/roles.py); an empty role uses chat's."""
        pick = self.conversation_models.get(conversation_id or "")
        pick = pick if pick and self.model(pick) is not None else None
        base = self.default_chain(conversation_id)
        return [pick] + [r for r in base if r != pick] if pick else base

    def default_chain(self, conversation_id: str | None = None) -> list[str]:
        """The list of the conversation's role, an empty role chat's: its Default."""
        role = self.conversation_roles.get(conversation_id or "", "chat")
        return self.chain(role) or self.chain("chat")

    def task_chain(self, role: str, conversation_id: str | None = None) -> list[str]:
        """A chat call runs on the conversation's chain, any other on its role's."""
        return self.chat_chain(conversation_id) if role == "chat" else self.chain(role)


# ---- validation (pure: raises Invalid with a field path and a sentence) -------------

def _typed(path: str, v: Any, t: str, choices) -> Any:
    ok = {"bool": isinstance(v, bool),
          "int": isinstance(v, int) and not isinstance(v, bool),
          "num": isinstance(v, (int, float)) and not isinstance(v, bool),
          "str": isinstance(v, str),
          "list": isinstance(v, list) and all(isinstance(x, str) for x in v)}[t]
    if not ok:
        want = {"bool": "true or false", "int": "a whole number", "num": "a number",
                "str": "text", "list": "a list of text values"}[t]
        raise Invalid(path, f"This must be {want}.")
    if t in ("int", "num") and v < 0:
        raise Invalid(path, "This cannot be negative.")
    if choices is not None and v not in choices:
        shown = ", ".join(repr(c) for c in choices)
        raise Invalid(path, f"This must be one of {shown}.")
    return v


def validate_provider(pid: str, p: Any, path: str = "") -> dict[str, Any]:
    """Returns the cleaned provider. Unknown keys are refused, so a typo never
    silently does nothing."""
    base = path or f"providers.{pid}"
    if not ID.match(pid):
        raise Invalid(base, "A provider id is letters, digits, '-', '_' or '.', "
                            "starting with a letter or digit.")
    if not isinstance(p, dict):
        raise Invalid(base, "A provider is an object.")
    for k in p:
        if k not in _PROVIDER_KEYS:
            raise Invalid(f"{base}.{k}", f"'{k}' is not a provider setting.")
    kind = p.get("kind")
    if kind not in kinds_mod.KINDS:
        raise Invalid(f"{base}.kind", "The kind must be one of "
                      + ", ".join(kinds_mod.KINDS) + ".")
    out: dict[str, Any] = {"kind": kind}
    label = p.get("label", pid)
    if not isinstance(label, str) or not label.strip() or len(label) > 80:
        raise Invalid(f"{base}.label", "The name must be 1 to 80 characters.")
    out["label"] = label.strip()
    url = kinds_mod.normalize_url(kind, str(p.get("base_url") or ""))
    if not url or not kinds_mod.valid_url(url):
        raise Invalid(f"{base}.base_url", "That is not an address. Try "
                                          "http://192.168.1.20:11434 or similar.")
    out["base_url"] = url
    opts_in = p.get("options") or {}
    if not isinstance(opts_in, dict):
        raise Invalid(f"{base}.options", "Options are an object.")
    schema = OPTIONS[kind]
    opts: dict[str, Any] = {}
    for k, v in opts_in.items():
        if k not in schema:
            raise Invalid(f"{base}.options.{k}", f"'{k}' is not an option for {kind}.")
        t, _, choices = schema[k]
        opts[k] = _typed(f"{base}.options.{k}", v, t, choices)
        if k == "thinking_levels":
            _check_levels(f"{base}.options.{k}", v)
    if opts:
        out["options"] = opts
    models_in = p.get("models") or {}
    if not isinstance(models_in, dict):
        raise Invalid(f"{base}.models", "Models are an object keyed by the model name.")
    models: dict[str, Any] = {}
    for name, m in models_in.items():
        models[name] = validate_model(kind, name, m, f"{base}.models.{name}", url)
    out["models"] = models
    return out


def validate_model(kind: str, name: str, m: Any, path: str,
                   base_url: str) -> dict[str, Any]:
    if not MODEL.match(str(name)):
        raise Invalid(path, "A model name has no spaces or quotes.")
    if m is None:
        m = {}
    if not isinstance(m, dict):
        raise Invalid(path, "A model is an object.")
    for k in m:
        if k not in _MODEL_KEYS:
            raise Invalid(f"{path}.{k}", f"'{k}' is not a model setting.")
    out: dict[str, Any] = {}
    label = m.get("label", name)
    if not isinstance(label, str) or len(label) > 120:
        raise Invalid(f"{path}.label", "The name must be at most 120 characters.")
    out["label"] = label or name
    caps = m.get("caps")
    if caps is None and kind == "comfyui":
        from ..comfyui import presets
        caps = [presets.ROLES.get(presets.kind_of(name) or "images")]
    elif caps is None:
        caps = kinds_mod.guess_caps(name)
    if not isinstance(caps, list) or any(c not in kinds_mod.CAPS for c in caps):
        raise Invalid(f"{path}.caps", "Capabilities are some of "
                      + ", ".join(kinds_mod.CAPS) + ".")
    media = {"image_generation", "music_generation"}
    if (kind == "comfyui") != bool(media & set(caps)) or (
            kind == "comfyui" and len(set(caps)) > 1):
        raise Invalid(f"{path}.caps", "A ComfyUI preset has one capability, "
                                      "image_generation or music_generation, and only "
                                      "ComfyUI has them.")
    out["caps"] = list(dict.fromkeys(caps))
    for k in ("context", "slots"):
        if m.get(k) not in (None, 0):
            out[k] = _typed(f"{path}.{k}", m[k], "int", None)
    for k in ("params", "llama"):
        v = m.get(k)
        if v in (None, {}):
            continue
        if not isinstance(v, dict):
            raise Invalid(f"{path}.{k}", f"{k} is an object of request fields.")
        if k == "llama" and kind not in kinds_mod.LLAMA:
            raise Invalid(f"{path}.{k}", "llama options only apply to llama-swap, tabbyAPI "
                                         "and llama-server.")
        out[k] = v
    eff = m.get("reasoning_effort")
    if eff not in (None, ""):
        if kind != "openrouter":
            raise Invalid(f"{path}.reasoning_effort", "Reasoning effort is an OpenRouter "
                                                      "option. For other kinds, set it in "
                                                      "params.")
        from .thinking import valid
        if not valid(eff):
            raise Invalid(f"{path}.reasoning_effort", "An effort is one short word, such "
                                                      "as medium or xhigh.")
        out["reasoning_effort"] = eff
    tl = m.get("thinking_levels")
    if tl not in (None, []):
        _check_levels(f"{path}.thinking_levels", tl)
        out["thinking_levels"] = list(dict.fromkeys(tl))
    if m.get("zdr"):
        if kind != "openrouter":
            raise Invalid(f"{path}.zdr", "Zero data retention is an OpenRouter option.")
        out["zdr"] = _typed(f"{path}.zdr", m["zdr"], "bool", None)
    slug = m.get("provider")
    if slug not in (None, ""):
        if kind != "openrouter":
            raise Invalid(f"{path}.provider", "A provider slug is an OpenRouter option.")
        if not isinstance(slug, str) or len(slug) > 64 or not PROVIDER_SLUG.match(slug):
            raise Invalid(f"{path}.provider", "A provider slug is lowercase letters, digits, "
                                              "'.', '_', '-' and at most one '/', such as "
                                              "deepinfra/fp4.")
        out["provider"] = slug
    twin = m.get("vision_twin")
    if twin not in (None, ""):
        # the same weights loaded with the projector under another name on the same
        # server (llama-swap "my-model-vision"): a request with images names it
        if kind not in kinds_mod.LLAMA:
            raise Invalid(f"{path}.vision_twin", "A vision twin is a llama-swap, tabbyAPI "
                                                 "or llama-server option.")
        if not isinstance(twin, str) or not MODEL.match(twin) or twin == name:
            raise Invalid(f"{path}.vision_twin", "The vision twin is another model name on "
                                                 "the same server, such as my-model-vision.")
        out["vision_twin"] = twin
        if "vision" not in out["caps"]:
            out["caps"].append("vision")
    if m.get("max_tokens") not in (None, 0):
        if kind != "anthropic":
            raise Invalid(f"{path}.max_tokens", "Max tokens is an Anthropic option. For other "
                                                "kinds, set it in params.")
        n = _typed(f"{path}.max_tokens", m["max_tokens"], "int", None)
        top = kinds_mod.server_output(base_url, name)
        if top is not None and n > top:
            raise Invalid(f"{path}.max_tokens", f"Max tokens is 1 to {top:,}.")
        out["max_tokens"] = n
    if m.get("cache_ttl") not in (None, ""):
        if kind != "anthropic":
            raise Invalid(f"{path}.cache_ttl", "The cache time is an Anthropic option.")
        out["cache_ttl"] = _typed(f"{path}.cache_ttl", m["cache_ttl"], "str", CACHE_TTLS)
    return out


def _check_levels(path: str, tl: Any) -> None:
    from .thinking import OFF, valid
    if not isinstance(tl, list) or len(tl) > 16 or not all(valid(x) for x in tl):
        raise Invalid(path, "Thinking levels are up to 16 short words, such as "
                            "[\"low\", \"high\", \"xhigh\"].")
    if OFF in tl:
        raise Invalid(path, "\"off\" is always offered. Leave it out of the list.")


def validate_providers(data: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(data, dict):
        raise Invalid("providers", "Providers are an object keyed by provider id.")
    out = {pid: validate_provider(pid, p) for pid, p in data.items()}
    for pid, p in out.items():
        shared = (p.get("options") or {}).get("shares_gpu_with")
        if not shared:
            continue
        other = out.get(shared)
        if other is None or other["kind"] not in kinds_mod.SWAPS:
            raise Invalid(f"providers.{pid}.options.shares_gpu_with",
                          f"{p['label']} shares its GPU with '{shared}', which is not a "
                          f"llama-swap or tabbyAPI provider. Choose one or none.")
    return out


def validate_role(role: str, r: Any, providers: dict[str, Any],
                  path: str = "") -> dict[str, Any]:
    """A role's model list. Empty is fine: the role is off (chat: not set)."""
    base = path or f"roles.{role}"
    if role not in ROLES:
        raise Invalid(base, f"There is no role called '{role}'. Roles: "
                      + ", ".join(ROLES) + ".")
    if not isinstance(r, dict):
        raise Invalid(base, "A role is an object with a chain.")
    for k in r:
        if k != "chain":
            raise Invalid(f"{base}.{k}", f"'{k}' is not a role setting.")
    spec = ROLES[role]
    chain = r.get("chain") or []
    if not isinstance(chain, list) or any(not isinstance(x, str) for x in chain):
        raise Invalid(f"{base}.chain", "The chain is a list of models.")
    if len(set(chain)) != len(chain):
        raise Invalid(f"{base}.chain", "A model is in the list twice.")
    if len(chain) > 6:
        raise Invalid(f"{base}.chain", "At most 6 models per role.")
    for i, ref in enumerate(chain):
        where = f"{base}.chain[{i}]"
        prov, _, model = ref.partition("/")
        p = providers.get(prov)
        if p is None:
            raise Invalid(where, f"There is no provider '{prov}'.")
        m = (p.get("models") or {}).get(model)
        if m is None:
            raise Invalid(where, f"{p.get('label', prov)} has no model '{model}'. Add it "
                                 f"to the provider first.")
        lacks = [c for c in spec["caps"] if c not in kinds_mod.caps_of(p, model)]
        if lacks:
            raise Invalid(where, f"{m.get('label', model)} cannot do "
                                 f"{spec['label']}: the model has no "
                                 f"'{lacks[0]}' capability.")
    return {"chain": list(chain)}


def validate_roles(data: Any, providers: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(data, dict):
        raise Invalid("roles", "Roles are an object keyed by role.")
    return {role: validate_role(role, r, providers) for role, r in data.items()}


def validate_triggers(data: Any) -> dict[str, dict[str, Any]]:
    """triggers.toml: per event a skill declares (celine/triggers) `on`. Which names
    exist is the skills' (the API checks it); a table for an event no skill declares any
    more is kept and does nothing."""
    if not isinstance(data, dict):
        raise Invalid("triggers", "Triggers are an object keyed by trigger.")
    out: dict[str, dict[str, Any]] = {}
    for name, t in data.items():
        if not isinstance(t, dict) or set(t) - {"on"}:
            raise Invalid(f"triggers.{name}", "A trigger has `on` only.")
        if not isinstance(t.get("on", False), bool):
            raise Invalid(f"triggers.{name}.on", "This must be true or false.")
        out[str(name)] = {"on": bool(t.get("on", False))}
    return out


def validate_tools(data: Any) -> dict[str, str]:
    if not isinstance(data, dict):
        raise Invalid("defaults", "Tool defaults are an object: {target: \"on\"|\"off\"}.")
    out: dict[str, str] = {}
    for t, v in data.items():
        if not TARGET.match(str(t)):
            raise Invalid(f"defaults.{t}", "A target is a skill (chat_archive) or a tool "
                                           "(chat_archive__search).")
        if v not in ("on", "off"):
            raise Invalid(f"defaults.{t}", "A default is \"on\" or \"off\".")
        out[str(t)] = v
    return out


# ---- keys and backends -----------------------------------------------------------

def provider_key(pid: str, secrets_root) -> str | None:
    """The key typed into Settings (var/secrets/<provider>.key), or None."""
    from .secrets import read_key
    return read_key(secrets_root, pid) if secrets_root is not None else None


def provider_headers(s, pid: str) -> dict[str, str]:
    """The provider's key as its kind's API takes it; {} without one."""
    p = (getattr(s, "providers", None) or {}).get(pid) or {}
    return kinds_mod.headers(p.get("kind", "openai"),
                             provider_key(pid, getattr(s, "secrets_root", None)))


def key_state(pid: str, p: dict[str, Any], secrets_root) -> dict[str, Any]:
    """What the API may say about a key: set or not, never the key."""
    from .secrets import state
    st = state(secrets_root, pid) if secrets_root is not None else {"state": "not set"}
    need = kinds_mod.KINDS[p.get("kind", "openai")]["key"] == "required"
    if st["state"] == "set":
        return {"state": "set", "set_at": st.get("set_at"), "required": need}
    return {"state": "not set", "set_at": None, "required": need}


def backend_config(s: Settings, ref: str) -> BackendConfig | None:
    pid, name = s.split(ref)
    p = s.providers.get(pid)
    if p is None:
        return None
    m = (p.get("models") or {}).get(name)
    if m is None:
        return None
    kind = p["kind"]
    opts = p.get("options") or {}
    caps = kinds_mod.caps_of(p, name)
    raw: dict[str, Any] = {"kind": kinds_mod.KINDS[kind]["backend"], "provider_kind": kind,
                           "images": "vision" in caps, "tools": "tools" in caps}
    if m.get("context"):
        raw["context"] = m["context"]
    if opts.get("timeout"):
        raw["timeout"] = float(opts["timeout"])
    params = dict(copy.deepcopy(m.get("params") or {}))
    if kind in kinds_mod.LLAMA and m.get("llama"):
        raw["llama"] = copy.deepcopy(m["llama"])
    if m.get("vision_twin"):
        raw["vision_twin"] = m["vision_twin"]
    if kind == "ollama" and opts.get("keep_alive"):
        params.setdefault("keep_alive", opts["keep_alive"])
    if kind == "anthropic":
        params["max_tokens"] = m.get("max_tokens") or params.get("max_tokens") or MAX_TOKENS
        if m.get("cache_ttl"):
            raw["cache_ttl"] = m["cache_ttl"]
    if kind == "openrouter":
        routing = {"models": list(opts.get("backup_models") or []),
                   "service_tier": opts.get("service_tier", "")}
        prov = {"order": opts.get("order") or [], "only": opts.get("only") or [],
                "ignore": opts.get("ignore") or [],
                "allow_fallbacks": opts.get("allow_fallbacks", True),
                "sort": "" if opts.get("order") else opts.get("sort", ""),
                "require_parameters": opts.get("require_parameters", True),
                "data_collection": opts.get("data_collection", "deny"),
                "zdr": bool(m.get("zdr", False)),
                "quantizations": opts.get("quantizations") or []}
        if m.get("provider"):
            # pinned to one endpoint: the provider's order, sort and fallbacks do not apply
            prov["order"] = [m["provider"]]
            prov["sort"] = ""
            prov["allow_fallbacks"] = False
        raw["routing"] = routing
        raw["provider"] = prov
        eff = m.get("reasoning_effort")
        if eff:
            params["reasoning"] = {"effort": eff}
    return BackendConfig(
        id=ref, kind=raw["kind"], label=p.get("label", pid),
        base_url=str(p["base_url"]).rstrip("/"), model=name,
        params=params, headers={}, raw=raw,
        _api_key=provider_key(pid, s.secrets_root))


def compile_into(cfg, s: Settings) -> None:
    """The few values from var/settings that config/ carries (your name, what you wrote
    about you, the time zone), and `cfg.settings` itself, which everything else reads (tool defaults: skills/gating;
    the index refresh: kb/semantic.py; the photo descriptions switch: kb/media.py)."""
    from . import general as general_mod
    cfg.user = general_mod.value(s.general, "user_name")
    cfg.bio = general_mod.value(s.general, "user_bio").strip()
    cfg.timezone = general_mod.value(s.general, "timezone")
    cfg.settings = s
