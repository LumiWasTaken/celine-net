"""Per provider kind: address normalisation, model discovery and the connection test.

Kinds: llama-swap, tabby (tabbyAPI behind the modality router: llama-swap's /running
for what is loaded), llama-server, ollama, openrouter, anthropic, openai (any
OpenAI-compatible), and comfyui (images and music: its "models" are the presets it can
run, comfyui/presets.py). A kind with a `default_url` lives at that one address.
Discovery and the test take an optional httpx transport so tests never touch a network.
"""
from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..backends.openai import no_credits

# Per kind: its label, the backend that talks to it (backends/openai.py build), whether
# it needs a key, its one address when it has one, where the router asks for its state
# (`probe`: the path under the address root, "/..." on the server root), how that answer
# lists the loaded models (`loaded`: llama-swap's /running, Ollama's /api/ps, a /models
# list; None: it does not say), and how often its model list is read for the context it
# states (`discover_every` seconds; None: never).
KINDS: dict[str, dict[str, Any]] = {
    "llama-swap": {"label": "llama-swap", "backend": "llama-server", "key": "optional",
                   "probe": "/running", "loaded": "running", "discover_every": 60.0},
    "tabby": {"label": "tabbyAPI", "backend": "llama-server", "key": "optional",
              "probe": "/running", "loaded": "running", "discover_every": 60.0},
    "llama-server": {"label": "llama-server", "backend": "llama-server", "key": "optional",
                     "probe": "models", "loaded": "models", "discover_every": 60.0},
    "ollama": {"label": "Ollama", "backend": "ollama", "key": "optional",
               "probe": "/api/ps", "loaded": "ps", "discover_every": None},
    # /models answers without a key; /key checks it
    "openrouter": {"label": "OpenRouter", "backend": "openrouter", "key": "required",
                   "default_url": "https://openrouter.ai/api/v1",
                   "probe": "key", "loaded": None, "discover_every": 3600.0},
    "anthropic": {"label": "Anthropic", "backend": "anthropic", "key": "required",
                  "default_url": "https://api.anthropic.com/v1",
                  "probe": "models", "loaded": None, "discover_every": 3600.0},
    "openai": {"label": "OpenAI-compatible", "backend": "openai", "key": "optional",
               "probe": "models", "loaded": None, "discover_every": 60.0},
    "comfyui": {"label": "ComfyUI", "backend": "comfyui", "key": "optional",
                "probe": "/system_stats", "loaded": None, "discover_every": None},
}
# llama-swap's API (/running, /upstream/<m>/, /api/models/unload): llama-swap itself and
# the modality router in front of tabbyAPI
SWAPS = ("llama-swap", "tabby")
LLAMA = (*SWAPS, "llama-server")
CAPS = ("chat", "tools", "vision", "embeddings", "rerank", "image_generation",
        "music_generation")
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_BETAS = "thinking-binding-controls-2026-08-01"
EFFORTS = ("low", "medium", "high", "xhigh", "max")     # Anthropic's, weakest first


def address(raw: str, hosted: bool = False) -> str:
    """The address as typed, with a scheme and without a trailing slash or an endpoint:
    `192.168.1.20:11434/v1/chat/completions` -> `http://192.168.1.20:11434/v1`."""
    url = (raw or "").strip()
    if url and "://" not in url:
        url = ("https://" if hosted else "http://") + url
    url = url.rstrip("/")
    for tail in ("/chat/completions", "/completions", "/embeddings"):
        if url.endswith(tail):
            url = url[: -len(tail)]
    return url


def normalize_url(kind: str, raw: str) -> str:
    """Forgiving input, one saved form: `192.168.1.20:11434` -> `http://192.168.1.20:11434/v1`."""
    default = KINDS.get(kind, {}).get("default_url")
    url = address(raw, bool(default))
    if not url and default:
        return default
    if kind not in ("openrouter", "comfyui") and urlsplit(url).path in ("", "/"):
        url = url + "/v1"
    return url


_HOST = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?)+$|^\[?[0-9a-fA-F:]+\]?$")


def valid_url(url: str) -> bool:
    raw = url
    if any(c.isspace() for c in raw):
        return False
    try:
        u = urlsplit(raw)
        u.port                       # raises on a port that is not a number
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.hostname) and bool(
        _HOST.match(u.hostname or ""))


def root_of(base_url: str) -> str:
    """http://h:p/v1 -> http://h:p (Ollama's /api and llama-swap's /running live there)."""
    u = base_url.rstrip("/")
    return re.sub(r"/v1$", "", u)



# The last resort, for a model its server says nothing about: a word in its name. Wrong
# guesses are fixed in Settings, Models (the model's capabilities).
NAMED_CAPS = (("rerank", ["rerank"]), ("embed", ["embeddings"]),
              ("vision", ["chat", "tools", "vision"]), ("-vl", ["chat", "tools", "vision"]))


def guess_caps(model_id: str, name: str = "") -> list[str]:
    s = f"{model_id} {name}".lower()
    return next((list(caps) for word, caps in NAMED_CAPS if word in s), ["chat", "tools"])


def reported_caps(m: dict[str, Any]) -> list[str] | None:
    """The caps a /models entry states itself (llama-swap, OpenRouter, TabbyAPI): image
    input is vision, a tools parameter or function calling is tools. None when it states
    nothing."""
    arch = m.get("architecture") if isinstance(m.get("architecture"), dict) else {}
    told = m.get("capabilities") if isinstance(m.get("capabilities"), dict) else {}
    params = set(m.get("supported_parameters") or [])
    mparams = m.get("parameters") if isinstance(m.get("parameters"), dict) else {}
    mods = m.get("modalities") if isinstance(m.get("modalities"), dict) else {}
    if not (arch or told or params or "use_vision" in mparams or "vision" in mods):
        return None
    caps = ["chat"]
    if "tools" in params or told.get("function_calling") or (not arch and not told and not params):
        caps.append("tools")
    if ("image" in (arch.get("input_modalities") or []) or told.get("vision")
            or mparams.get("use_vision") or mods.get("vision")):
        caps.append("vision")
    return caps


# ---- discovery ---------------------------------------------------------------------

_cache: dict[tuple, tuple[float, list[dict]]] = {}
CACHE_SECONDS = 60.0


def _client(transport=None, timeout: float = 8.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=transport)


def headers(kind: str, key: str | None) -> dict[str, str]:
    """The key as the kind's API takes it: Anthropic's own headers, else a bearer token."""
    if kind == "anthropic":
        return {"anthropic-version": ANTHROPIC_VERSION, "anthropic-beta": ANTHROPIC_BETAS,
                **({"x-api-key": key} if key else {})}
    return {"Authorization": f"Bearer {key}"} if key else {}


async def discover(kind: str, base_url: str, key: str | None = None, *,
                   refresh: bool = False, transport=None) -> list[dict[str, Any]]:
    """[{id, name, caps, context?, output?, loaded?, price?, thinking_levels?}] from the
    provider itself. Raises httpx.HTTPError / ValueError when the provider cannot be asked."""
    ck = (kind, base_url, bool(key))
    hit = _cache.get(ck)
    if hit and not refresh and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    url = base_url.rstrip("/")
    async with _client(transport) as c:
        if kind == "ollama":
            out = await _ollama(c, url)
        elif kind == "comfyui":
            from ..comfyui import server as comfyui
            out = await comfyui.discover(c, url, headers(kind, key))
        elif kind == "openrouter":
            out = await _openrouter(c, url, key)
        elif kind == "anthropic":
            out = await _anthropic(c, url, key)
        else:
            r = await c.get(f"{url}/models", headers=headers(kind, key))
            r.raise_for_status()
            out = []
            for m in r.json().get("data") or []:
                if not isinstance(m, dict) or not m.get("id"):
                    continue
                mid = str(m["id"])
                name = str(m.get("name") or "")
                entry: dict[str, Any] = {"id": mid, "name": name or mid,
                                         "caps": reported_caps(m) or guess_caps(mid, name)}
                mparams = m.get("parameters") if isinstance(m.get("parameters"), dict) else {}
                meta = m.get("meta") if isinstance(m.get("meta"), dict) else {}
                status = (m.get("status") or {}) if isinstance(m.get("status"), dict) else {}
                # llama-server's router mode lists each model's command line (`args`)
                ctx = (m.get("context_length") or mparams.get("max_seq_len") or meta.get("n_ctx")
                       or cmd_window(" ".join(str(a) for a in status.get("args") or [])))
                if isinstance(ctx, int) and ctx > 0:
                    entry["context"] = ctx
                eff = (m.get("reasoning") or {}).get("supported_efforts") if isinstance(
                    m.get("reasoning"), dict) else None
                if eff or "reasoning" in (m.get("supported_parameters") or []):
                    entry["thinking_levels"] = [str(x) for x in (eff or ["low", "medium", "high"])
                                                if str(x) not in ("none", "off")]
                if status.get("value"):
                    entry["loaded"] = status["value"] == "loaded"
                out.append(entry)
            if kind in SWAPS:
                loaded = await _running(c, root_of(url))
                for e in out:
                    if loaded is not None:
                        e["loaded"] = e["id"] in loaded
                        if loaded.get(e["id"]):
                            e["context"] = loaded[e["id"]]   # what it really runs with
    out.sort(key=lambda e: e["id"].lower())
    _cache[ck] = (time.monotonic(), out)
    return out


def cmd_window(cmd: str | None) -> int | None:
    """The context one request gets from a llama-server command line (llama-swap
    `/running` shows it): `-c/--ctx-size`. With `--parallel N` the slots split it,
    unless `--kv-unified`, where each request may use the whole of it. None when the
    command line does not say: `--fit` sizes the context at load time and `--fit-ctx`
    is only the least it may shrink to, so a `--fit` line says nothing (the loaded
    server's /props does, backends/router.py)."""
    args = (cmd or "").split()
    def val(*names: str) -> int | None:
        for i, a in enumerate(args[:-1]):
            if a in names and args[i + 1].isdigit():
                return int(args[i + 1])
        return None
    n = val("-c", "--ctx-size")
    if not n:
        return None
    parallel = val("-np", "--parallel") or 1
    return n if "--kv-unified" in args or "-kvu" in args else n // parallel


async def _running(c: httpx.AsyncClient, root: str) -> dict[str, int | None] | None:
    """{model: cmd_window} of the models llama-swap has loaded."""
    try:
        r = await c.get(f"{root}/running", timeout=3.0)
        return {str(x.get("model")): cmd_window(x.get("cmd"))
                for x in r.json().get("running", [])
                if x.get("state") not in ("starting", "stopping")}
    except (httpx.HTTPError, ValueError, AttributeError):
        return None


async def _ollama(c: httpx.AsyncClient, url: str) -> list[dict[str, Any]]:
    root = root_of(url)
    r = await c.get(f"{root}/api/tags")
    r.raise_for_status()
    out = []
    for m in (r.json().get("models") or [])[:200]:
        mid = str(m.get("name") or m.get("model") or "")
        if not mid:
            continue
        entry: dict[str, Any] = {"id": mid, "name": mid, "caps": guess_caps(mid)}
        try:
            s = (await c.post(f"{root}/api/show", json={"model": mid}, timeout=5.0)).json()
            caps = s.get("capabilities") or []
            mapped = []
            if "completion" in caps:
                mapped.append("chat")
            if "tools" in caps:
                mapped.append("tools")
            if "vision" in caps:
                mapped.append("vision")
            if "embedding" in caps:
                mapped.append("embeddings")
            if "thinking" in caps:
                entry["thinking_levels"] = ["low", "medium", "high"]
            if mapped:
                entry["caps"] = mapped
            for k, v in (s.get("model_info") or {}).items():
                if k.endswith(".context_length") and isinstance(v, int):
                    entry["context"] = v
        except (httpx.HTTPError, ValueError, AttributeError):
            pass                        # no details: the name's guess stands
        out.append(entry)
    return out


async def _openrouter(c: httpx.AsyncClient, url: str, key: str | None) -> list[dict[str, Any]]:
    r = await c.get(f"{url}/models", headers=headers("openrouter", key))
    r.raise_for_status()
    out = []
    for m in r.json().get("data") or []:
        mid = str(m.get("id") or "")
        if not mid:
            continue
        entry: dict[str, Any] = {"id": mid, "name": str(m.get("name") or mid),
                                 "caps": reported_caps(m) or ["chat"]}
        if isinstance(m.get("context_length"), int):
            entry["context"] = m["context_length"]
        top = m.get("top_provider") if isinstance(m.get("top_provider"), dict) else {}
        if isinstance(top.get("max_completion_tokens"), int):
            entry["output"] = top["max_completion_tokens"]
        pr = m.get("pricing") or {}
        try:
            entry["price"] = {"in_per_m": round(float(pr.get("prompt", 0)) * 1e6, 4),
                              "out_per_m": round(float(pr.get("completion", 0)) * 1e6, 4)}
        except (TypeError, ValueError):
            pass
        eff = (m.get("reasoning") or {}).get("supported_efforts") if isinstance(
            m.get("reasoning"), dict) else None
        if "reasoning" in (m.get("supported_parameters") or []):
            entry["thinking_levels"] = [str(x) for x in (eff or ["low", "medium", "high"])
                                        if str(x) not in ("none", "off")]
        out.append(entry)
    return out


async def _anthropic(c: httpx.AsyncClient, url: str, key: str | None) -> list[dict[str, Any]]:
    """Every page of /models (`has_more`, `after_id`). Every Claude model takes images and
    tools; its context is `max_input_tokens`, its output `max_tokens`, its thinking words
    the efforts it lists."""
    out: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        r = await c.get(f"{url}/models", headers=headers("anthropic", key),
                        params={"limit": 100, **({"after_id": after} if after else {})})
        r.raise_for_status()
        page = r.json()
        for m in page.get("data") or []:
            mid = str(m.get("id") or "")
            if not mid:
                continue
            entry: dict[str, Any] = {"id": mid, "name": str(m.get("display_name") or mid),
                                     "caps": ["chat", "tools", "vision"]}
            if isinstance(m.get("max_input_tokens"), int):
                entry["context"] = m["max_input_tokens"]
            if isinstance(m.get("max_tokens"), int):
                entry["output"] = m["max_tokens"]
            effort = (m.get("capabilities") or {}).get("effort") or {}
            levels = [w for w in EFFORTS if (effort.get(w) or {}).get("supported")]
            if levels:
                entry["thinking_levels"] = levels
            out.append(entry)
        after = page.get("last_id")
        if not page.get("has_more") or not after:
            return out


# ---- what runs at an address ---------------------------------------------------------

async def detect(base_url: str, key: str | None = None, *,
                 transport=None) -> tuple[str | None, bool, str]:
    """(the kind of server at `base_url`, did anything answer at all, the address that
    answered), from what it answers: tabbyAPI has /v1/model (or /.well-known/serviceinfo)
    and wins over the /running of the router in front of it, llama-swap has /running,
    Ollama /api/tags, llama-server lists its models as owned by llamacpp, any other
    /models is OpenAI-compatible. ComfyUI answers /system_stats. A kind's own address
    (OpenRouter, Anthropic) is that kind. An address with no path that answers as no
    known kind is tried once more with `/v1` appended. Only reads lists; never loads a
    model."""
    kind, answered = await _detect(base_url, key, transport)
    if kind is None and urlsplit(base_url).path in ("", "/"):
        retry = base_url.rstrip("/") + "/v1"
        kind, more = await _detect(retry, key, transport)
        if kind:
            return kind, True, retry
        answered = answered or more
    return kind, answered, base_url


async def _detect(base_url: str, key: str | None, transport) -> tuple[str | None, bool]:
    host = (urlsplit(base_url).hostname or "").lower()
    for kind, k in KINDS.items():
        if k.get("default_url") and urlsplit(k["default_url"]).hostname == host:
            return kind, True
    root = root_of(base_url)
    answered = False
    async with _client(transport, timeout=5.0) as c:
        async def listed(url: str, field: str, headers: dict | None = None,
                         shape: type = list):
            nonlocal answered
            try:
                r = await c.get(url, headers=headers or {})
            except httpx.HTTPError:
                return None
            answered = True
            try:
                data = r.json() if r.status_code == 200 else None
            except ValueError:
                return None
            got = data.get(field) if isinstance(data, dict) else None
            return got if isinstance(got, shape) else None

        if await listed(f"{base_url.rstrip('/')}/model", "id", headers("openai", key),
                        str) is not None or await listed(
                f"{root}/.well-known/serviceinfo", "software", shape=dict) is not None:
            return "tabby", True
        if await listed(f"{root}/running", "running") is not None:
            return "llama-swap", True
        if await listed(f"{root}/api/tags", "models") is not None:
            return "ollama", True
        if await listed(f"{root}/system_stats", "devices") is not None:
            return "comfyui", True
        models = await listed(f"{base_url.rstrip('/')}/models", "data",
                              headers("openai", key))
        if models is not None:
            if any(isinstance(m, dict) and m.get("owned_by") == "llamacpp" for m in models):
                return "llama-server", True
            return "openai", True
    return None, answered


# ---- connection test ---------------------------------------------------------------

async def check(kind: str, base_url: str, key: str | None, *, key_required: bool,
               transport=None) -> dict[str, Any]:
    """{ok, level: ok|needs_you, sentence?, models?, loaded?}. One plain sentence, only
    when something is wrong."""
    url = base_url
    host = urlsplit(url).netloc or url
    if key_required and not key:
        return {"ok": False, "level": "needs_you", "fix": "add_key",
                "sentence": "No API key yet."}
    try:
        models = await discover(kind, base_url, key, refresh=True, transport=transport)
    except httpx.TimeoutException:
        return {"ok": False, "level": "needs_you",
                "sentence": f"No answer from {host} within 8 s."}
    except httpx.ConnectError:
        return {"ok": False, "level": "needs_you",
                "sentence": f"Not reachable: connection refused at {host}."}
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code in (401, 403):
            return {"ok": False, "level": "needs_you",
                    "sentence": f"The server rejected the key ({code}).", "fix": "replace_key"}
        if no_credits(code, e.response.text):
            return {"ok": False, "level": "needs_you",
                    "sentence": f"{KINDS[kind]['label']} is out of credits."}
        return {"ok": False, "level": "needs_you", "sentence": f"The server answered {code}."}
    except (httpx.HTTPError, ValueError) as e:
        return {"ok": False, "level": "needs_you",
                "sentence": f"Not reachable: {type(e).__name__} at {host}."}
    # all is fine: no sentence, the page says nothing
    out: dict[str, Any] = {"ok": True, "level": "ok", "models": len(models)}
    loaded = [m["id"] for m in models if m.get("loaded")]
    if loaded:
        out["loaded"] = loaded
    if kind == "openrouter":
        try:
            async with _client(transport) as c:
                r = await c.get(f"{url.rstrip('/')}/key", headers=headers(kind, key))
            if r.status_code in (401, 403):
                return {"ok": False, "level": "needs_you", "fix": "replace_key",
                        "sentence": f"OpenRouter rejected the key ({r.status_code})."}
        except httpx.HTTPError:
            pass                        # /models answered: the key is checked next time
    return out
