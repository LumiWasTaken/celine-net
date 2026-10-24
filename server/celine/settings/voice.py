"""Banned words: the words its chat models are steered from (its row's `voice`,
shipped in config/her/voice.toml).

One list, `banned`, one weight, `bias`. On tabbyAPI (its chat) the banned words and the
`phrases` go into the request as `banned_strings`, plain text the engine backtracks over,
case-insensitive, any length. On llama-swap and llama-server each banned word goes into
`logit_bias` as a string with `bias` as its weight (-100 bans it outright); there a word
is best one token with its leading space (" delve"), as every token of the string is
biased. backends/openai.py LlamaServerBackend sends both. Only its turns (every kind of
conversation) and the writer (its own words, skills/writer.py) get them: tools, vision
and embeddings answer in their own words. `on = false` sends none.

`phrases` are what a banned word cannot reach ("couldn't help but"): plain text, found
by turns/slop.py and, on tabbyAPI, banned as it streams. `patterns` are regexes slop.py
only counts ("not X, but Y").
"""
from __future__ import annotations

import copy
import re
from typing import Any

from .files import Invalid
from .kinds import LLAMA

ROLES = ("chat", "writer")
DEFAULT = {"banned": [], "bias": -8.0, "on": True, "phrases": [], "patterns": []}
MAX_BANS = 1000
MAX_PHRASES = 500


def validate(data: Any) -> dict[str, Any]:
    """The whole section; a key left out is its default."""
    if not isinstance(data, dict):
        raise Invalid("voice", "The voice settings are an object.")
    for k in data:
        if k not in DEFAULT:
            raise Invalid(f"voice.{k}", f"'{k}' is not a voice setting.")
    words = data.get("banned", [])
    if not isinstance(words, list) or len(words) > MAX_BANS:
        raise Invalid("voice.banned", f"This is a list of at most {MAX_BANS} words.")
    for i, b in enumerate(words):
        if not isinstance(b, str) or not b.strip() or len(b) > 64 or "\n" in b:
            raise Invalid(f"voice.banned[{i}]", "A banned word is one line of 1 to 64 "
                                                "characters, like \" delve\".")
    lists = {"banned": list(dict.fromkeys(words))}
    for key in ("phrases", "patterns"):
        items = data.get(key, [])
        if not isinstance(items, list) or len(items) > MAX_PHRASES:
            raise Invalid(f"voice.{key}", f"This is a list of at most {MAX_PHRASES} entries.")
        for i, x in enumerate(items):
            if not isinstance(x, str) or not x.strip() or len(x) > 200 or "\n" in x:
                raise Invalid(f"voice.{key}[{i}]", "Each entry is one line of 1 to 200 "
                                                   "characters.")
            if key == "patterns":
                try:
                    re.compile(x)
                except re.error as e:
                    raise Invalid(f"voice.{key}[{i}]", f"This pattern does not read: "
                                                       f"{e}.") from None
        lists[key] = list(dict.fromkeys(items))
    bias = data.get("bias", DEFAULT["bias"])
    if isinstance(bias, bool) or not isinstance(bias, (int, float)) or \
            not -100 <= bias <= 100:
        raise Invalid("voice.bias", "The weight is a number from -100 to 100.")
    on = data.get("on", DEFAULT["on"])
    if not isinstance(on, bool):
        raise Invalid("voice.on", "This must be true or false.")
    return {**lists, "bias": float(bias), "on": on}


def of(cfg, role: str) -> dict | None:
    """The voice of a call of `role`: its turns (a chat call, whatever role the
    conversation's kind runs on: a mission, a sub-agent and a background run are its own
    too) and the writer speak with its Banned words; any other call has none."""
    if role not in ROLES or getattr(cfg, "her", None) is None:
        return None
    return cfg.her.voice


def apply(bc, voice: dict[str, Any]):
    """A copy of the BackendConfig with its voice for its request: on tabbyAPI
    `raw["banned_strings"]` (its phrases and Banned words, as LlamaServerBackend sends
    them), on llama-swap and llama-server `raw["voice"]` (the [string, bias] pairs it
    sends as `logit_bias`); the same object when they do not apply."""
    kind = (bc.raw or {}).get("provider_kind")
    if not voice.get("on") or kind not in LLAMA:
        return bc
    if kind == "tabby":
        strings = list(dict.fromkeys([*voice.get("phrases", []), *voice.get("banned", [])]))
        if not strings:
            return bc
        out = copy.copy(bc)
        out.raw = {**bc.raw, "banned_strings": strings}
        return out
    if not voice.get("banned"):
        return bc
    out = copy.copy(bc)
    out.raw = {**bc.raw, "voice": [[b, voice.get("bias", DEFAULT["bias"])]
                                   for b in voice["banned"]]}
    return out


def merge_voice(base: dict, added: dict | None) -> dict:
    """A base's Banned words with `added` laid over it: the words of both, each once, an
    added plain word given a leading space (" delve", the way it stands mid-sentence);
    any other key `added` gives wins."""
    added = added or {}
    new = [f" {w}" if isinstance(w, str) and w[:1].isalnum() else w
           for w in added.get("banned") or []]
    return {**base, **added,
            "banned": list(dict.fromkeys([*(base.get("banned") or []), *new]))}
