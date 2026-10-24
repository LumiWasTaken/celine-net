"""Text to token numbers: the only place that does it.

Estimates are characters per token by kind of text, starting from the Qwen
tokenizer (archive tool results run at 1.7 to
2.7 chars/token, median ~2.2; prose ~3; tool schemas ~3.8). Every model call reports `usage.prompt_tokens`; `observe()` moves a
per-model correction factor towards it, so the estimate converges on the real template.

The window (n_ctx of one slot) is what the server says, wherever it says it: a loaded
llama-swap model's /props (`running_windows`, read by every router probe), a
context-overflow error that states the server's n_ctx (`learn_window`), else the
context the provider reports for the model (`reported_windows`, its model list, read
by the router's ping). The server wins over the model's context size in Settings,
Models: that number counts only where no server says (and on Ollama, which runs with
it as num_ctx: its reported number is the model's most, not what it runs with).
Without any of them, the window is FALLBACK_WINDOW. Everything here is keyed by the
model that answered, on its server (`_key`).
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any

log = logging.getLogger("celine.tokens")

RATIO = {"tool": 2.2, "prose": 3.0, "schema": 3.8}
PER_MESSAGE = 6                  # chat-template overhead per message
FALLBACK_WINDOW = 32_768
RESULT_SHARE = 0.30              # one tool result may take this much of the window
MARGIN = 0.05                    # estimate error below the exact band


IMAGE_PATCH = 32      # pixels per token on each side (Qwen-VL style encoders), roughly
IMAGE_FALLBACK = 800  # an image of unknown size
IMAGE_MAX = 1600      # one image at most: encoders scale a large one down


def image_tokens(part: dict) -> int:
    """What one image part costs: its size in patches, when the part says (an
    attachment's `size`), else a flat guess."""
    size = (part.get("attachment") or {}).get("size") if isinstance(part, dict) else None
    if not size:
        return IMAGE_FALLBACK
    w, h = size
    return min(IMAGE_MAX, max(64, math.ceil(w / IMAGE_PATCH) * math.ceil(h / IMAGE_PATCH)))


class Counter:
    def __init__(self, scale: float = 1.0):
        self.scale = scale           # actual / estimated, learned from usage

    def estimate(self, text: str | None, kind: str = "prose") -> int:
        if not text:
            return 0
        return int(math.ceil(len(text) / RATIO.get(kind, RATIO["prose"]) * self.scale))

    def message(self, m: dict[str, Any]) -> int:
        kind = "tool" if m.get("role") == "tool" else "prose"
        c = m.get("content")
        n = PER_MESSAGE
        n += self.estimate(m.get("reasoning_content") or "", "prose")
        if isinstance(c, str):
            n += self.estimate(c, kind)
        elif isinstance(c, list):            # multimodal parts
            for part in c:
                if isinstance(part, dict) and part.get("type") == "text":
                    n += self.estimate(part.get("text") or "", kind)
                else:
                    n += image_tokens(part)
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            n += self.estimate((fn.get("name") or "") + (fn.get("arguments") or ""),
                               "tool") + 8
        return n

    def request(self, messages: list[dict], tools: list[dict] | None = None) -> int:
        n = sum(self.message(m) for m in messages)
        if tools:
            n += self.estimate(json.dumps(tools, ensure_ascii=False), "schema")
        return n

    def observe(self, estimated: int, actual: int) -> None:
        """Move the correction towards what the server counted (EMA, bounded)."""
        if estimated <= 0 or actual <= 0:
            return
        raw = self.scale * actual / estimated
        self.scale = min(3.0, max(0.33, 0.7 * self.scale + 0.3 * raw))


_counters: dict[str, Counter] = {}
_windows: dict[tuple[str, str], tuple[int, int | None]] = {}   # n_ctx, Settings then
_running: dict[tuple[str, str], int] = {}   # n_ctx of a loaded model, from /props
_reported: dict[tuple[str, str], int] = {}  # the context its provider's model list states
_outputs: dict[tuple[str, str], int] = {}   # the most one answer may take, the same list says
_current = Counter()


def counter(backend=None) -> Counter:
    """The calibrated counter of one model (a backend, keyed like its window: the
    model that answered, on its server; or a plain model name); the last used one
    when backend is None."""
    global _current
    if backend is None:
        return _current
    key = backend if isinstance(backend, str) else "/".join(_key(backend))
    c = _counters.setdefault(key, Counter())
    _current = c
    return c


def estimate(text: str | None, kind: str = "prose") -> int:
    """Estimate with the current model's calibration. For tools and the archive,
    which do not know which model reads their output."""
    return _current.estimate(text, kind)


def reset() -> None:
    """Tests."""
    global _current
    _counters.clear()
    _windows.clear()
    _running.clear()
    _reported.clear()
    _outputs.clear()
    _current = Counter()


# ---- the window ------------------------------------------------------------------------
def _key(backend) -> tuple[str, str]:
    """(server, model) of the model that answered this turn's last call, when one
    did; the head of the chain before that. What a fallback model taught (its window,
    its usage) stays with the fallback model."""
    backend = getattr(backend, "answered", None) or backend
    cfg = getattr(backend, "cfg", None)
    return (str(getattr(cfg, "base_url", "") or getattr(backend, "name", "")).rstrip("/"),
            str(getattr(backend, "model", "")))


def pinned_window(backend) -> int | None:
    return _pinned(getattr(getattr(backend, "answered", None) or backend, "cfg", None))


def _pinned(cfg) -> int | None:
    raw = getattr(cfg, "raw", None) or {}
    v = raw.get("context") if isinstance(raw, dict) else None   # Settings → Models → context
    try:
        return int(v) if v else None
    except (TypeError, ValueError):
        return None


def known_window(backend) -> int | None:
    pinned = pinned_window(backend)
    return (server_window(*_key(backend), pinned) or pinned
            or getattr(backend, "context_window", None))     # a test's fake backend


def config_window(cfg) -> int | None:
    """known_window of a model by its BackendConfig, for a caller with no backend (the
    shellbox's model proxy, api/llm_proxy.py)."""
    pinned = _pinned(cfg)
    return server_window(cfg.base_url, cfg.model, pinned) or pinned


def server_window(base_url: str, model: str, pinned: int | None = None) -> int | None:
    """What the server says one model runs with: loaded now (/props, an overflow's
    n_ctx under the same Settings; the smaller when both), else its provider's model
    list; None when no server says."""
    key = (base_url.rstrip("/"), model)
    learned, under = _windows.get(key, (None, None))
    now = [n for n in (_running.get(key), learned if under == pinned else None) if n]
    return min(now) if now else _reported.get(key)


def reported_windows(base_url: str, windows: dict[str, int]) -> bool:
    """The context each model of one provider states in its model list ({model: n},
    the router's discovery), replacing what that provider said before. True when
    that changed something."""
    base_url = base_url.rstrip("/")
    old = {k[1]: n for k, n in _reported.items() if k[0] == base_url}
    for model in old:
        del _reported[(base_url, model)]
    for model, n in windows.items():
        _reported[(base_url, model)] = n
    return old != windows


def reported_outputs(base_url: str, outputs: dict[str, int]) -> None:
    """The most one answer may take, as one provider's model list states it per model
    ({model: n}, the router's discovery), replacing what it said before."""
    base_url = base_url.rstrip("/")
    for key in [k for k in _outputs if k[0] == base_url]:
        del _outputs[key]
    _outputs.update({(base_url, m): n for m, n in outputs.items()})


def server_output(base_url: str, model: str) -> int | None:
    return _outputs.get((base_url.rstrip("/"), model))


def running_windows(base_url: str, windows: dict[str, int]) -> None:
    """What the models loaded on one server run with now ({model: n_ctx}, from each
    loaded model's /props, read by every router probe). A model that is not loaded
    any more forgets what was learned about it, the running value and an overflow's:
    its next load may size its context differently (`--fit`), and an overflow a vision
    twin answered must not stay on the text model."""
    for key in [k for k in {*_running, *_windows} if k[0] == base_url]:
        if key[1] not in windows:
            _running.pop(key, None)
            _windows.pop(key, None)
    for model, n in windows.items():
        _running[(base_url, model)] = n


def window(backend) -> int:
    return known_window(backend) or FALLBACK_WINDOW


def learn_window(backend, n_ctx: int | None) -> None:
    """The server's own n_ctx for this model, as long as Settings keeps saying what
    it says now."""
    if n_ctx and n_ctx > 0:
        _windows[_key(backend)] = (int(n_ctx), pinned_window(backend))


def max_output(backend) -> int:
    cfg = getattr(backend, "cfg", None)
    params = getattr(cfg, "params", None) or {}
    try:
        return int(params.get("max_tokens") or 4096)
    except (TypeError, ValueError):
        return 4096


def reserve(backend) -> int:
    """What a request leaves free in the window: its reply's room and the margin."""
    return max_output(backend) + int(window(backend) * MARGIN)


def result_cap(win: int) -> int:
    """The most one tool result may take, in tokens."""
    return max(1000, int(win * RESULT_SHARE))


def cut_to(text: str, max_tokens: int, kind: str = "tool") -> tuple[str, int, int]:
    """(text cut to about max_tokens, tokens shown, tokens total). Cuts at a line."""
    total = estimate(text, kind)
    if total <= max_tokens:
        return text, total, total
    chars = int(len(text) * max_tokens / total)
    head = text[:chars]
    nl = head.rfind("\n")
    if nl > chars // 2:
        head = head[:nl]
    return head, estimate(head, kind), total
