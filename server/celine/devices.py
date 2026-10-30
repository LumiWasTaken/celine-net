"""Her devices: each plugin that is on with a [drones] block in its plugin.toml, named
by the plugin's id. The block: `url` and `password` of its `opencode serve` (filled from
its form, settings/form.fill), `key` (its secret that is its key to the app's model
proxy, api/llm_proxy.py), `label`, `log` (where its server logs) and `share` (its share
server, optional: shares.py, the password its bearer). Drones run there
(sub_agents/drone.py)."""
from __future__ import annotations

from dataclasses import dataclass


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
        label = _label(p)
        out[p.id] = Device(p.id, label, url, password, key, str(d.get("log", "")), share,
                           f"{label}: {'; '.join(dict.fromkeys(empty))}." if empty else None)
    return out


def label(device: str) -> str:
    """A device's label, on or not; its name when no plugin has it."""
    from . import plugins
    p = plugins.get(device)
    return _label(p) if p is not None and isinstance(p.manifest.get("drones"), dict) \
        else device


def _label(p) -> str:
    return str(p.manifest["drones"].get("label") or p.manifest["label"])
