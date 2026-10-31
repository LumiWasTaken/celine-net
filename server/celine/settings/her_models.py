"""The models it may use itself: in its model tool (skills/models.py), as a
sub-agent's model (sub_agents/skill.py) and through the model proxy its shellbox's
agent CLIs call (api/llm_proxy.py).

There is no list of its own: every model in the saved providers that can chat and
whose provider has its key is its own, in the providers' order.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from .kinds import KINDS, caps_of

LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal")


def allowed(s) -> list[str]:
    """Every model in the providers that can chat (settings/kinds.py caps_of) and has
    its key, in their order."""
    return [f"{pid}/{name}" for pid, p in s.providers.items()
            if not s.key_missing(pid)
            for name in (p.get("models") or {}) if "chat" in caps_of(p, name)]


def label(s, ref: str) -> str:
    """A model as it reads it: its label and its provider's."""
    m = s.model(ref) or {}
    p = s.providers.get(s.split(ref)[0]) or {}
    return f"{m.get('label') or s.split(ref)[1]} on {p.get('label') or s.split(ref)[0]}"


def cloud(s, ref: str) -> bool:
    """A model whose provider's address (its base_url, else its kind's `default_url`)
    is not on this network: someone else's cloud runs it. Local: loopback, private,
    link-local and ULA addresses, a single-label host, and the .local, .lan,
    .home.arpa and .internal names."""
    p = s.providers.get(s.split(ref)[0]) or {}
    url = p.get("base_url") or KINDS.get(p.get("kind"), {}).get("default_url") or ""
    return not local_host(urlsplit(url if "://" in url else f"http://{url}").hostname or "")


def local_host(host: str) -> bool:
    """A host name or address on this network (cloud)."""
    host = host.rstrip(".").lower()
    if not host or host == "localhost" or "." not in host and ":" not in host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host.endswith(LOCAL_SUFFIXES)
    return ip.is_loopback or ip.is_private or ip.is_link_local
