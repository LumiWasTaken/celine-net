"""Settings the app writes: var/settings/*.toml (providers, roles, tools, general,
email, vault, triggers, appearance, guard, features, services) and var/secrets/*.key..

boot.load() calls `apply(cfg)` once config/ is read: `load(cfg)` reads each
var/settings file, validates it and the result goes on the Config (settings/registry.py). A file that does
not exist yet is empty: a fresh install has no providers and no task models, and the
Models page is where they are set up. A broken or invalid var/settings file is a fatal
Problem: a reload keeps the last good values.
"""
from __future__ import annotations

from typing import Any

from .. import plugins
from ..config import ConfigError, Problem, shown_path
from . import features as features_mod, files, form, general as general_mod
from . import mail as mail_mod, services as services_mod, vault as vault_mod
from .files import Invalid
from .registry import (
    Settings, compile_into, validate_providers, validate_roles, validate_tools,
    validate_triggers,
)
from .secrets import secrets_dir

# per-conversation chat models, cid -> "provider/model". Shared by every Settings
# object (a reload builds a new one); filled from the database at startup and kept in
# step by settings/conversations.py.
CONVERSATION_MODELS: dict[str, str] = {}
# per-conversation thinking, cid -> "off" or an effort word (conversations.thinking)
CONVERSATION_THINKING: dict[str, str] = {}
# per-conversation role, cid -> its role (settings/roles.py resolve)
CONVERSATION_ROLES: dict[str, str] = {}


def file_to_section(section: str, data: dict[str, Any]) -> Any:
    if section == "providers":
        return data.get("providers") or {}
    if section == "tools":
        return data.get("defaults") or {}
    return data


def section_to_file(section: str, value: Any) -> dict[str, Any]:
    if section == "providers":
        return {"providers": value}
    if section == "tools":
        return {"defaults": value}
    if section == "general":            # a skill's knobs in a table of its own
        out: dict[str, Any] = {}
        for k, v in value.items():
            skill, dot, name = k.partition(".")
            if dot:
                out.setdefault(skill, {})[name] = v
            else:
                out[k] = v
        return out
    return value


def load(cfg) -> Settings:
    root = files.settings_dir(cfg)
    s = Settings(secrets_root=secrets_dir(cfg.data_dir),
                 conversation_models=CONVERSATION_MODELS,
                 conversation_thinking=CONVERSATION_THINKING,
                 conversation_roles=CONVERSATION_ROLES)

    def pick(section: str, validate):
        """var/settings/<section>.toml, or the empty section while it does not exist."""
        raw, err = files.read(root, section)
        where = shown_path(files.path_for(root, section))
        if err:                   # a file that does not parse: celine exits 2 (__main__)
            raise ConfigError(f"{where}: {err}")
        if raw is not None:
            try:
                value = validate(file_to_section(section, raw))
            except Invalid as e:
                s.problems.append(Problem(where, f"{e.field}: {e.message}", fatal=True))
            else:
                s.saved[section] = True
                return value
        s.saved[section] = False
        return validate({})

    s.providers = pick("providers", validate_providers)
    s.roles = pick("roles", lambda v: validate_roles(v, s.providers))
    s.tools = pick("tools", validate_tools)
    s.general = pick("general", lambda v: general_mod.validate(v))
    s.email = pick("email", mail_mod.validate)
    s.vault = pick("vault", vault_mod.validate)
    s.triggers = pick("triggers", validate_triggers)
    from ..guard import rules as guard_rules
    s.guard = pick("guard", guard_rules.validate)
    known = set(features_mod.ids())          # a removed plugin's switch goes with it
    s.features = pick("features", lambda v: features_mod.validate(
        {k: x for k, x in v.items() if k in known or k == "done"}))
    if not s.saved["features"]:          # nothing that needs a service before Setup saved it
        s.features = features_mod.seed()
    s.services = pick("services", services_mod.validate)
    if not s.saved["services"]:          # the environment, read nowhere else
        s.services = services_mod.seed()
    for p in plugins.with_form():        # var/settings/<id>.toml
        s.plugins[p.id] = pick(p.id, lambda v, p=p: form.validate(p.id, p.form["fields"], v))
    return s


def apply(cfg) -> None:
    """Load and compile into cfg. A broken file is a ConfigError, an invalid one a fatal
    Problem (a reload keeps the last good values)."""
    s = load(cfg)
    cfg.problems.extend(s.problems)
    compile_into(cfg, s)


def compile_her(cfg) -> None:
    """The names people read (intercom/store.py NAMES): the user's and its own."""
    from ..intercom import store as intercom_rows
    intercom_rows.NAMES.update(user=cfg.user or "you", her=cfg.name)


__all__ = ["CONVERSATION_MODELS", "CONVERSATION_ROLES",
           "CONVERSATION_THINKING", "Invalid", "Settings", "apply", "compile_her",
           "load",
           "file_to_section", "section_to_file"]
