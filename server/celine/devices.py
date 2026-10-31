"""Her devices: each plugin that is on with a [drones] block in its plugin.toml, named
by the plugin's id. The block: `url` and `password` of its `opencode serve` (filled from
its form, settings/form.fill), `key` (its secret that is its key to the app's model
proxy, api/llm_proxy.py), `label`, `log` (where its server logs), `share` (its share
server, optional: shares.py, the password its bearer) and `about` (what the device
reported of itself, optional: its user, home, chip, memory, system, network; in her
system prompt, `about`). Drones run there (sub_agents/drone.py)."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass
class Device:
    id: str
    label: str
    url: str
    password: str
    key: str
    log: str
    share: str             # its share server's address, "" without one
    why: str | None        # why no drone starts on it now, None when one does
    # its hosts: those of its drones, shares and plugin servers (the gateway) addresses
    hosts: frozenset[str] = frozenset()
    about: str = ""        # what it reported of itself, "" before it did


def devices(cfg) -> dict[str, Device]:
    """Every device that is on, by name."""
    from . import plugins
    from .settings import features, form
    out = {}
    for p in plugins.all():
        d = p.manifest.get("drones")
        if not isinstance(d, dict) or not features.on(cfg, p.id):
            continue
        fields = (p.form or {}).get("fields", [])
        empty: list[str] = []
        url = form.fill(cfg, p.id, fields, str(d.get("url", "")), empty)
        password = form.fill(cfg, p.id, fields, str(d.get("password", "")), empty)
        share = form.fill(cfg, p.id, fields, str(d.get("share", "")), [])
        key = (form.read_secret(cfg, p.id, d["key"]) or "") if d.get("key") else ""
        label = cfg.substitute(_label(p))
        servers = [form.fill(cfg, p.id, fields, str(s.get("url", "")), [])
                   for s in (p.manifest.get("servers") or {}).values() if isinstance(s, dict)]
        out[p.id] = Device(p.id, label, url, password, key, str(d.get("log", "")), share,
                           f"{label}: {'; '.join(dict.fromkeys(empty))}." if empty else None,
                           _hosts([url, share, *servers]),
                           form.fill(cfg, p.id, fields, str(d.get("about", "")), []))
    return out


def about(cfg) -> str | None:
    """What each device that is on reported of itself, one line each; None without any."""
    lines = [f"{d.label}: {d.about}." for d in devices(cfg).values() if d.about]
    return "\n".join(lines) or None


def hosts(cfg) -> frozenset[str]:
    """The hosts of every device that is on: hers, never a way out of her box."""
    return frozenset().union(*(d.hosts for d in devices(cfg).values()))


def _hosts(urls) -> frozenset[str]:
    out = set()
    for u in urls:
        try:
            h = urlsplit(u if "//" in u else "//" + u).hostname
        except ValueError:
            continue
        if h:
            out.add(h.casefold())
    return frozenset(out)


def label(device: str, cfg=None) -> str:
    """A device's label, on or not; its name when no plugin has it. With `cfg` its
    `{{name}}` filled; without, a prompt's own substitute fills it."""
    from . import plugins
    p = plugins.get(device)
    out = _label(p) if p is not None and isinstance(p.manifest.get("drones"), dict) \
        else device
    return cfg.substitute(out) if cfg is not None else out


def _label(p) -> str:
    return str(p.manifest["drones"].get("label") or p.manifest["label"])
