"""Every write of var/settings: one lock, validate what changes, write the changed
sections; reload.commit reloads in place and says so on the global feed
(`settings.changed`)."""
from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any

from .. import plugins
from . import address as address_mod, features as features_mod, files, form
from . import general as general_mod
from . import mail as mail_mod, section_to_file, services as services_mod
from . import vault as vault_mod
from .registry import validate_providers, validate_role, validate_tools, validate_triggers
from ..core.event_types import SETTINGS_CHANGED

log = logging.getLogger("celine.settings")
_lock: asyncio.Lock | None = None


def lock() -> asyncio.Lock:
    """Held around a read-check-write of var/settings, so two writes never interleave."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _current(s) -> dict[str, Any]:
    return {"providers": copy.deepcopy(s.providers), "roles": copy.deepcopy(s.roles),
            "tools": dict(s.tools), "general": dict(s.general), "email": dict(s.email),
            "vault": dict(s.vault), "triggers": copy.deepcopy(s.triggers),
            "features": dict(s.features),
            "guard": copy.deepcopy(s.guard), "services": copy.deepcopy(s.services),
            "address": dict(s.address),
            **{p.id: dict(s.plugins.get(p.id, {})) for p in plugins.with_form()}}


def _validate(new: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    """Each section that differs from `old` validated, the rest as stored: a save never
    fails on a setting it does not touch (a role whose model lost a capability since).
    A role is checked when it changed or a provider in its chain did."""
    from ..guard import rules as guard_rules
    checks = {"tools": validate_tools, "general": general_mod.validate,
              "email": mail_mod.validate, "vault": vault_mod.validate,
              "triggers": validate_triggers, "features": features_mod.validate,
              "guard": guard_rules.validate, "services": services_mod.validate,
              "address": address_mod.validate,
              **{p.id: (lambda d, p=p: form.validate(p.id, p.form["fields"], d))
                 for p in plugins.with_form()}}
    out = {k: (fn(new[k]) if new[k] != old[k] else new[k]) for k, fn in checks.items()}
    providers = new["providers"]
    if providers != old["providers"]:
        providers = validate_providers(providers)
    moved = {pid for pid in set(providers) | set(old["providers"])
             if providers.get(pid) != old["providers"].get(pid)}
    out["providers"] = providers
    out["roles"] = {
        role: validate_role(role, r, providers)
        if r != old["roles"].get(role)
        or any(ref.partition("/")[0] in moved for ref in (r or {}).get("chain") or [])
        else r
        for role, r in new["roles"].items()}
    return out


def check(cfg, changes: dict[str, Any]) -> dict[str, Any]:
    """Validate `changes` (section -> whole new value) on top of the current settings,
    as a save does, writing nothing. Raises Invalid; -> every section, clean."""
    old = _current(cfg.settings)
    return _validate({**old, **changes}, old)


def save(cfg, changes: dict[str, Any]) -> list[str]:
    """Validate `changes` (section -> whole new value) on top of the current settings
    and write the sections they change. Raises Invalid; -> the sections written. The
    caller reloads (reload.commit)."""
    clean = check(cfg, changes)
    root = files.settings_dir(cfg)
    written = []
    for section in (*files.SECTIONS, *(p.id for p in plugins.with_form())):
        if section in changes:
            files.write(root, section, section_to_file(section, clean[section]))
            written.append(section)
    log.info("settings written", extra={"event": "settings.written", "sections": written})
    return written


def publish(services, what: list[str]) -> None:
    if services.store is not None:
        services.store.emit_global({"type": SETTINGS_CHANGED, "what": what})
