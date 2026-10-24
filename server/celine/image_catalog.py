"""The diffusion models and LoRAs a ComfyUI server has, each with what it is for.

There is no list of models here (a few file name heuristics guess the category and
whether a model is turbo), and nothing is read from disk: this app may run in a
container, away from the model folders. What exists comes from ComfyUI (`/object_info`
of UNETLoader and LoraLoaderModelOnly); what each file is comes from
ComfyUI-LoRA-Manager's HTTP API on the same server (`/api/lm/...`), then the file name:

- name: LoRA-Manager's model name (Civitai's, or the one set there);
- summary: its notes; a line "settings: steps 28, cfg 3.5" sets a model's sampler;
- tags: Civitai's and your own; `celine-<category>` sets the category,
  `celine-nsfw` / `celine-sfw` override the nsfw guess, `celine-hidden` keeps it from
  Celine, `celine-turbo` / `celine-full` say whether a model is distilled;
- usage_tips (LoRAs): {"strength"}, the strength a LoRA given without one takes;
- trigger words; usage count;
- excluded in LoRA-Manager: dropped entirely, whatever ComfyUI lists.

A server without LoRA-Manager (its routes answer 404) has file names only; any other
answer of it fails the load with the reason.

All of it is edited in LoRA-Manager's UI or with its API (save-metadata, add-tags,
exclude). A file LoRA-Manager reports as 0 bytes is still downloading: listed, not
ready. A preset says which part of the catalog is its own in its [catalog] table (name
prefixes, like "krea2/" and "krea_2/"; comfyui/presets.py).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import httpx

from .comfyui.presets import options
from .comfyui.server import object_info

log = logging.getLogger("celine.image_catalog")

TTL = 300.0                 # seconds a server's catalog is reused
TIMEOUT = 20.0              # seconds one request to the server may take
PAGE = 100                  # LoRA-Manager entries per page asked
AT_ONCE = 8                 # trigger word requests in flight at once
KINDS = ("model", "lora")
CATEGORIES = ("character", "style", "realism", "nsfw", "anatomy", "concept", "tool",
              "base", "finetune", "edit")
NSFW_TAGS = {"nsfw", "explicit"}    # the catalog's own tags that mark the class
LOADERS = {"model": ("UNETLoader", "unet_name"),
           "lora": ("LoraLoaderModelOnly", "lora_name")}
_EXT = re.compile(r"\.(safetensors|gguf|ckpt|pt|pth|bin)$", re.I)


@dataclass(frozen=True)
class Resource:
    kind: str                   # model | lora
    name: str                   # ComfyUI's name, e.g. "style/my_style_v1.safetensors"
    label: str
    category: str
    summary: str = ""
    tags: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()
    nsfw: bool = False
    strength: float = 1.0
    settings: dict[str, Any] = field(default_factory=dict)
    turbo: bool | None = None
    hidden: bool = False
    ready: bool = True
    uses: int = 0

    @property
    def id(self) -> str:
        """The name without its extension: short, stable, unique."""
        return _EXT.sub("", self.name)

    def line(self) -> str:
        bits = [f"`{self.id}`", self.category, self.label]
        out = ", ".join(bits)
        if self.summary:
            out += f": {self.summary}"
        elif self.tags:
            out += f", tags: {', '.join(self.tags[:8])}"
        if self.triggers:
            out += f"; triggers: {', '.join(self.triggers)}"
        if self.kind == "lora":
            out += f"; strength {self.strength:g}"
        if self.kind == "model":
            if self.turbo is True:
                out += "; turbo (8 steps)"
            elif self.turbo is False:
                out += "; non-turbo (full quality, slower)"
        if self.nsfw:
            out += "; nsfw"
        if not self.ready:
            out += "; still downloading"
        return out


@dataclass
class Catalog:
    resources: list[Resource]
    loaded: float = field(default_factory=time.monotonic)

    def scope(self, kind: str, prefixes: list[str] | tuple[str, ...]) -> list[Resource]:
        return [r for r in self.resources if r.kind == kind and (
            not prefixes or any(r.name.startswith(p) for p in prefixes))]

    def list(self, kind: str, prefixes, *, category: str | None = None,
             search: str | None = None) -> list[Resource]:
        out = [r for r in self.scope(kind, prefixes) if not r.hidden]
        if category:
            out = [r for r in out if r.category == category.strip().lower()]
        if search:
            words = [w for w in re.findall(r"[\w-]+", search.lower()) if w]
            out = [r for r in out if all(any(t.startswith(w) for t in _haystack(r)) for w in words)]
        return sorted(out, key=lambda r: (CATEGORIES.index(r.category)
                                          if r.category in CATEGORIES else 99,
                                          -r.uses, r.label.lower()))

    def find(self, kind: str, ref: str, prefixes) -> Resource | None:
        """By id, ComfyUI name, file stem or label, case-insensitive. Hidden ones too:
        the caller decides (a preset's own LoRA is hidden but valid)."""
        want = ref.strip().lower()
        pool = self.scope(kind, prefixes)
        for r in pool:
            if want in (r.id.lower(), r.name.lower(), r.id.rsplit("/", 1)[-1].lower(),
                        r.label.lower()):
                return r
        return None

    def nearest(self, kind: str, ref: str, prefixes) -> list[str]:
        words = [w for w in re.split(r"[\W_]+", ref.lower()) if len(w) > 2]
        pool = self.list(kind, prefixes)
        scored = sorted(((sum(any(t.startswith(w) for t in _haystack(r)) for w in words), r.id) for r in pool),
                        reverse=True)
        return [i for s, i in scored[:3] if s]


def _haystack(r: Resource) -> set[str]:
    turbo_words = ["turbo"] if r.turbo is True else (["non-turbo", "full", "raw"] if r.turbo is False else [])
    text = " ".join([r.id, r.name, r.label, r.summary, r.category, *r.tags, *r.triggers, *turbo_words]).lower()
    return set(re.findall(r"[\w-]+", text))


# ---- loading ---------------------------------------------------------------------------

_cache: dict[str, Catalog] = {}


async def load(base_url: str, headers: dict[str, str] | None = None, *,
               transport: httpx.AsyncBaseTransport | None = None) -> Catalog:
    """The server's catalog, reused for TTL seconds."""
    key = base_url.rstrip("/")
    hit = _cache.get(key)
    if hit is not None and time.monotonic() - hit.loaded < TTL:
        return hit
    async with httpx.AsyncClient(base_url=key, headers=headers or {}, timeout=TIMEOUT,
                                 transport=transport) as c:
        names = {kind: await _names(c, *LOADERS[kind]) for kind in KINDS}
        lm, excluded = await _lora_manager(c)
        triggers = await _triggers(c, [n for n in names["lora"] if _key(n) in lm])
    out = []
    for kind in KINDS:
        for name in names[kind]:
            if _key(name) in excluded:
                continue
            r = _resource(kind, name, lm.get(_key(name)), triggers.get(name))
            if r is not None:
                out.append(r)
    cat = Catalog(out)
    _cache[key] = cat
    log.info("image catalog loaded", extra={
        "event": "image_catalog.loaded", "models": len(names["model"]),
        "loras": len(names["lora"]), "described": len(lm)})
    return cat


async def _names(c: httpx.AsyncClient, cls: str, key: str) -> list[str]:
    entry = (await object_info(c, "", {cls}))[cls] or {}
    spec = (entry.get("input") or {}).get("required", {})
    return [str(x) for x in options(spec.get(key)) or []]


def _key(name: str) -> str:
    return _EXT.sub("", name.replace("\\", "/")).lower()


async def _lm_get(c: httpx.AsyncClient, path: str, **params) -> dict | None:
    """One LoRA-Manager answer; None when it is not installed (404). Any other failure
    raises: httpx.HTTPStatusError, or ValueError for an answer that is not JSON."""
    r = await c.get(path, params=params)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json() or {}


async def _lora_manager(c: httpx.AsyncClient) -> tuple[dict[str, dict], set[str]]:
    """LoRA-Manager's entries by folder/stem, and the excluded ones; empty when it is
    not installed."""
    out: dict[str, dict] = {}
    excluded: dict[str, dict] = {}
    for path, into in (("/api/lm/loras/list", out), ("/api/lm/checkpoints/list", out),
                       ("/api/lm/loras/excluded", excluded),
                       ("/api/lm/checkpoints/excluded", excluded)):
        page = 1
        while True:
            data = await _lm_get(c, path, page=page, page_size=PAGE)
            if data is None:
                break
            items = data.get("items") or []
            for it in items:
                folder = str(it.get("folder") or "").strip("/")
                stem = str(it.get("file_name") or "")
                if stem:
                    into[f"{folder}/{stem}".lstrip("/").lower()] = it
            if not items or page * PAGE >= int(data.get("total") or 0):
                break
            page += 1
    return out, set(excluded)


async def _triggers(c: httpx.AsyncClient, loras: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    limit = anyio.Semaphore(AT_ONCE)

    async def one(name: str) -> None:
        stem = Path(_EXT.sub("", name)).name
        async with limit:
            data = await _lm_get(c, "/api/lm/loras/get-trigger-words", name=stem)
        words = (data or {}).get("trigger_words")
        if words:
            out[name] = [str(w).strip() for w in words if str(w).strip()]

    async with anyio.create_task_group() as tg:
        for n in loras:
            tg.start_soon(one, n)
    return out


def _resource(kind: str, name: str, lm: dict | None, triggers: list[str] | None
              ) -> Resource | None:
    lm = lm or {}
    all_tags = [str(t).lower() for t in (lm.get("tags") or [])]
    own = {t[7:] for t in all_tags if t.startswith("celine-")}
    tags = tuple(dict.fromkeys(t for t in all_tags if not t.startswith("celine-")))
    stem = Path(_EXT.sub("", name)).name
    label = str(lm.get("model_name") or stem)[:100]
    if "nsfw" in own or "sfw" in own:
        nsfw = "nsfw" in own
    else:
        nsfw = bool(NSFW_TAGS & set(tags)) or "nsfw" in name.lower()
    # celine-nsfw is also the flag, so it is the category only when no other is set
    category = next((c for c in CATEGORIES if c in own and c != "nsfw"), None) or (
        "nsfw" if kind == "lora" and "nsfw" in own else _category(kind, name, tags, nsfw))
    tips = _json(lm.get("usage_tips"))
    summary, settings = _notes(str(lm.get("notes") or ""))
    turbo = True if "turbo" in own else False if "full" in own else (
        True if is_turbo_name(name) else (False if kind == "model" else None))
    size = lm.get("file_size")
    return Resource(
        kind=kind, name=name, label=label, category=category, summary=summary,
        tags=tags, triggers=tuple(triggers or ()), nsfw=nsfw,
        strength=_num(tips.get("strength"), 1.0),
        settings=settings, turbo=turbo, hidden="hidden" in own,
        ready=not (isinstance(size, int) and size == 0),
        uses=int(lm.get("usage_count") or 0))


def _json(text: Any) -> dict:
    try:
        data = json.loads(text) if isinstance(text, str) and text.strip() else text
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _num(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


_SETTING = re.compile(r"^\s*settings\s*:\s*(.+)$", re.I | re.M)


def _notes(text: str) -> tuple[str, dict[str, Any]]:
    """The notes without their settings line, and the settings: "settings: steps 28,
    cfg 3.5, sampler euler, scheduler simple"."""
    settings: dict[str, Any] = {}
    m = _SETTING.search(text)
    if m:
        for part in m.group(1).split(","):
            k, _, v = part.strip().partition(" ")
            if k and v.strip():
                v = v.strip()
                settings[k.lower()] = (int(v) if v.isdigit() else float(v)
                                       if re.fullmatch(r"\d+\.\d+", v) else v)
        text = _SETTING.sub("", text)
    return " ".join(text.split())[:300], settings


def is_turbo_name(name: str) -> bool:
    """A distilled (fast, few steps) model says so in its file name: "turbo" (not
    "noturbo"). Anything else runs
    with the preset's full-quality settings."""
    n = name.lower().replace("_", "").replace("-", "")
    return ("turbo" in n and "noturbo" not in n)


def _category(kind: str, name: str, tags: tuple[str, ...], nsfw: bool) -> str:
    low = name.lower()
    t = set(tags)
    if kind == "model":
        if "edit" in low:
            return "edit"
        return "base" if re.search(r"(^|/)krea2_(turbo|raw)|base", low) else "finetune"
    if "tool" in t or re.search(r"distill|identity_edit|control|projector", low):
        return "tool"
    if t & {"character", "celebrity", "person"}:
        return "character"
    if "slider" in low or t & {"body", "anatomy", "slider"}:
        return "anatomy"
    if nsfw:
        return "nsfw"
    if t & {"realism", "realistic", "photorealistic", "photography"}:
        return "realism"
    if t & {"style", "artstyle", "art style", "anime"}:
        return "style"
    return "concept"
