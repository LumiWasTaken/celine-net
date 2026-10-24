"""The models it may use itself: in its model tool (skills/models.py), as a
sub-agent's model (sub_agents/skill.py) and through the model proxy its shellbox's
agent CLIs call (api/llm_proxy.py).

There is no list of its own: every model in the saved providers that can chat and
whose provider has its key is its own, in the providers' order.
"""
from __future__ import annotations


def allowed(s) -> list[str]:
    """Every model in the providers that can chat and has its key, in their order."""
    return [f"{pid}/{name}" for pid, p in s.providers.items()
            if not s.key_missing(pid)
            for name, m in (p.get("models") or {}).items() if "chat" in (m.get("caps") or [])]


def label(s, ref: str) -> str:
    """A model as it reads it: its label and its provider's."""
    m = s.model(ref) or {}
    p = s.providers.get(s.split(ref)[0]) or {}
    return f"{m.get('label') or s.split(ref)[1]} on {p.get('label') or s.split(ref)[0]}"
