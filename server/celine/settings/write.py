"""Every write of var/settings: one lock, validate everything, write the changed
sections; reload.commit reloads in place and says so on the global feed
(`settings.changed`)."""
from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any

from . import features as features_mod, files, general as general_mod
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
            "guard": copy.deepcopy(s.guard), "services": copy.deepcopy(s.services)}


def _validate_all(new: dict[str, Any]) -> dict[str, Any]:
    from ..guard import rules as guard_rules
    providers = validate_providers(new["providers"])
    roles = {}
    for role, r in new["roles"].items():
        roles[role] = validate_role(role, r, providers)
    return {"providers": providers, "roles": roles, "tools": validate_tools(new["tools"]),
            "general": general_mod.validate(new["general"]),
            "email": mail_mod.validate(new["email"]),
            "vault": vault_mod.validate(new["vault"]),
            "triggers": validate_triggers(new["triggers"]),
            "features": features_mod.validate(new["features"]),
            "guard": guard_rules.validate(new["guard"]),
            "services": services_mod.validate(new["services"])}


def save(cfg, changes: dict[str, Any]) -> list[str]:
    """Validate `changes` (section -> whole new value) on top of the current settings
    and write the sections they change. Raises Invalid; -> the sections written. The
    caller reloads (reload.commit)."""
    new = _current(cfg.settings)
    new.update(changes)
    clean = _validate_all(new)
    root = files.settings_dir(cfg)
    written = []
    for section in files.SECTIONS:
        if section in changes:
            files.write(root, section, section_to_file(section, clean[section]))
            written.append(section)
    log.info("settings written", extra={"event": "settings.written", "sections": written})
    return written


def publish(services, what: list[str]) -> None:
    if services.store is not None:
        services.store.emit_global({"type": SETTINGS_CHANGED, "what": what})
