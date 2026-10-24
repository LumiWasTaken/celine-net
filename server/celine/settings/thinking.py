"""One thinking setting per conversation: "off", or an effort word the chat model
accepts ("low", "high", "xhigh", ...). NULL keeps the model's own default.

The words differ per provider and model (one chat template takes low, medium and xhigh,
reads high as xhigh, and fails the request on any other word; another ignores the word; OpenRouter
lists `supported_efforts` per model), so nothing here is a fixed list: a
model's accepted words are `thinking_levels` on the model in Settings, else the
provider's `thinking_levels` option, else a default per kind. Any short token the user
enters is accepted and passed through as-is; the list is what the UI offers.
Background calls (captions, document descriptions, titles) set "off" for themselves
through `LLM.bind(thinking=...)`.

| kind                     | off                                         | any other word `w` |
|--------------------------|---------------------------------------------|--------------------|
| llama-swap, llama-server | `reasoning_effort: "none"`, `chat_template_kwargs.enable_thinking: false` | `enable_thinking: true`, `reasoning_effort: w` (llama-server gives it to the chat template) |
| ollama                   | `reasoning_effort: "none"` (/api/chat `think: false`) | `reasoning_effort: w` (/api/chat `think: w`, backends/ollama.py) |
| openrouter               | `reasoning: {effort: "none"}`               | `reasoning: {effort: w}` |
| openai                   | `reasoning_effort: "none"`                  | `reasoning_effort: w` |
| anthropic                | `output_config.effort`: the lowest word the model lists, no `thinking` (newer Claude models cannot stop thinking, so off thinks least and a short call is not cut off) | `thinking: {type: "adaptive", display: "summarized"}`, `output_config.effort: w`, `max_tokens` at least ROOM[w] + 1024 |

Checked against llama.cpp tools/server/server-common.cpp (reasoning_effort "none"
disables thinking, any other value goes to the template as `reasoning_effort`).
Anthropic: current Claude models refuse `budget_tokens` and take the effort word
instead.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from .kinds import LLAMA

OFF = "off"
TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,23}$")

# what the UI offers when neither the model nor its provider says (documented values)
DEFAULT_LEVELS: dict[str, list[str]] = {
    "llama-swap": ["low", "medium", "xhigh"],      # what Qwen chat templates take
    "tabby": ["low", "medium", "xhigh"],
    "llama-server": ["low", "medium", "xhigh"],
    "ollama": ["low", "medium", "high"],
    "openrouter": ["minimal", "low", "medium", "high", "xhigh"],
    "openai": ["minimal", "low", "medium", "high"],
    "anthropic": ["low", "medium", "high", "xhigh"],
}
# Anthropic: the tokens of max_tokens left for thinking at each effort
ROOM = {"low": 2048, "medium": 8192, "high": 16384, "xhigh": 32768, "max": 65536}

# the words a mode or the panel picks (no word: the model's own default); each is sent as
# a level the model takes (fit)
WORDS = (OFF, "low", "medium", "high", "max")
SCALE = ("minimal", "low", "medium", "high", "xhigh")


def fit(word: str, offered: list[str]) -> str:
    """The level sent for a word: the word itself when the model lists it (or when it is
    no neutral word, typed by hand with /think), "max" its strongest, else its strongest
    at or below the word, else its weakest. The panel stores the plain word."""
    have = [w for w in offered if w != OFF]
    if word == OFF or word not in WORDS or word in have:
        return word
    if not have:
        return "high" if word == "max" else word
    if word == "max":
        return have[-1]
    below = [w for w in have if w in SCALE and SCALE.index(w) <= SCALE.index(word)]
    return max(below, key=SCALE.index) if below else have[0]


def valid(value: Any) -> bool:
    return isinstance(value, str) and bool(TOKEN.match(value))


def levels(provider: dict[str, Any], model: dict[str, Any]) -> list[str]:
    """The words to offer for this model: its own list, the provider's, or the kind's."""
    own = model.get("thinking_levels")
    if own:
        return list(own)
    prov = (provider.get("options") or {}).get("thinking_levels")
    if prov:
        return list(prov)
    return list(DEFAULT_LEVELS.get(provider.get("kind", "openai"), []))


def apply(bc, value: str | None, offered: list[str] = ()):
    """A copy of the BackendConfig with the conversation's thinking in its request
    fields; the same object when there is none. `offered`: the model's words (levels)."""
    if not value or not valid(value):
        return bc
    kind = (bc.raw or {}).get("provider_kind", "openai")
    out = copy.copy(bc)
    params: dict[str, Any] = dict(copy.deepcopy(bc.params))
    raw: dict[str, Any] = dict(copy.deepcopy(bc.raw))
    for k in ("reasoning_effort", "reasoning", "think"):
        params.pop(k, None)
    effort = "none" if value == OFF else value
    if kind in LLAMA:
        llama = dict(raw.get("llama") or {})
        kw = dict(llama.get("chat_template_kwargs") or {})
        kw["enable_thinking"] = value != OFF
        llama["chat_template_kwargs"] = kw
        llama.pop("reasoning_effort", None)
        raw["llama"] = llama
        params["reasoning_effort"] = effort
    elif kind == "openrouter":
        params["reasoning"] = {"effort": effort}
    elif kind == "anthropic":
        params.pop("thinking", None)
        word = value if value != OFF else min(
            (w for w in offered if w in SCALE), key=SCALE.index, default=None)
        if word:
            params["output_config"] = {**(params.get("output_config") or {}), "effort": word}
        if value != OFF:
            params["thinking"] = {"type": "adaptive", "display": "summarized",
                                  "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
            params["max_tokens"] = max(int(params.get("max_tokens") or 0),
                                       ROOM.get(value, 0) + 1024)
    else:                                            # ollama, openai
        params["reasoning_effort"] = effort
    out.params = params
    out.raw = raw
    return out
