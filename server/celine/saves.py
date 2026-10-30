"""The saves of the Settings screens, one function each: the routes (api/settings.py,
api/forms.py) and her settings skill (skills/settings.py) save through the same one.
Each holds write.lock, checks `if_match` (the version the saver saw; None: no check; a
stale one raises files.Conflict), validates, commits (reload.commit) and does what
follows that save (runners, a new vault login). -> reload.commit's answer."""
from __future__ import annotations

import copy
from typing import Any

from . import plugins, reload
from .errors import NotFound
from .settings import features as features_mod, files, form, general as general_mod
from .settings import mail as mail_mod, secrets as secrets_mod, seed
from .settings import address as address_mod, services as services_mod, vault as vault_mod
from .settings import views, write
from .settings.files import Invalid
from .settings.registry import ROLES, validate_provider

KEEP = object()     # a secret left out of a save: the saved one stays


def _root(app):
    return secrets_mod.secrets_dir(app.cfg.data_dir)


async def general(app, values: Any, if_match: str | None = None) -> dict:
    async with write.lock():
        files.check_version("general", app.cfg.settings.general, if_match)
        return await reload.commit(app, {"general": general_mod.validate(values)})


async def features(app, values: Any, done: bool = False,
                   if_match: str | None = None) -> tuple[dict, dict]:
    """-> (commit's answer, the probes the save saw: the view shows them)."""
    async with write.lock():
        s = app.cfg.settings
        files.check_version("features", s.features, if_match)
        new = features_mod.validate(values)
        if done or features_mod.done(s.features):
            new["done"] = True
        hit = app.feature_probes
        probes = hit[1] if hit else await features_mod.probe_all(app.cfg, app)
        out = await reload.commit(app, {"features": new})
        await views.apply_runners(app)
    return out, probes


async def roles(app, roles: dict[str, Any], if_match: str | None = None) -> dict:
    """Every role at once; a role left out is emptied (off)."""
    async with write.lock():
        files.check_version("roles", app.cfg.settings.roles, if_match)
        return await reload.commit(app, {"roles": roles})


async def role(app, role: str, body: Any, if_match: str | None = None) -> dict:
    async with write.lock():
        s = app.cfg.settings
        if role not in ROLES:
            raise NotFound("role")
        files.check_version("role", s.roles.get(role), if_match)
        roles = copy.deepcopy(s.roles)
        roles[role] = body
        return await reload.commit(app, {"roles": roles})


def tool_set(tools: dict[str, str], target: str, default: str | None) -> dict[str, str]:
    """`tools` with one target's default: a skill ("chat_archive") or a tool
    ("chat_archive__search"); None removes the entry. A skill's clears the entries of its
    tools (the skill's switch is the bulk verb)."""
    if default not in ("on", "off", None):
        raise Invalid(f"tools.{target}", "A default is \"on\", \"off\" or null.")
    tools = dict(tools)
    if default is None:
        tools.pop(target, None)
        return tools
    tools[target] = default
    if "__" not in target:
        for t in [t for t in tools if t.startswith(target + "__")]:
            del tools[t]
    return tools


async def tools(app, changes: dict[str, str | None], if_match: str | None = None) -> dict:
    """`changes`: target -> "on", "off" or None, applied in order (tool_set)."""
    async with write.lock():
        files.check_version("tools", app.cfg.settings.tools, if_match)
        new = app.cfg.settings.tools
        for target, default in changes.items():
            new = tool_set(new, target, default)
        return await reload.commit(app, {"tools": new})


async def addresses(app, values: Any, if_match: str | None = None) -> dict:
    """The services' addresses; a service whose address changed starts again while its
    feature is on."""
    async with write.lock():
        s = app.cfg.settings
        files.check_version("services", s.services, if_match)
        clean = services_mod.validate(values)
        changed = {sid for sid in services_mod.IDS if services_mod.value(clean, sid, "url")
                   != services_mod.value(s.services, sid, "url")}
        out = await reload.commit(app, {"services": clean})
        await views.apply_runners(app, changed)
    return out


async def address(app, values: Any, if_match: str | None = None) -> dict:
    """The app's own address (settings/address.py): Settings, General, and the first
    login or setup (auth/routes.py). Written and set in place, no reload: nothing else
    is built from it, every reader asks for it when it runs."""
    async with write.lock():
        files.check_version("address", app.cfg.settings.address, if_match)
        write.save(app.cfg, {"address": values})
        app.cfg.settings.address = address_mod.validate(values)
        write.publish(app, ["address"])
    return {"applied": True}


def _secret(path: str, v: Any, said: str = "This must be text.") -> None:
    if v is not None and (not isinstance(v, str) or not v.strip()):
        raise Invalid(path, said)


async def email(app, values: Any, password: Any = KEEP, if_match: str | None = None) -> dict:
    """`password`: a string sets it, None removes it, KEEP keeps it."""
    if password is not KEEP:
        _secret("password", password, "The password must be text.")
    async with write.lock():
        files.check_version("email", app.cfg.settings.email, if_match)
        clean = mail_mod.validate(values)
        if password is None:
            secrets_mod.delete_key(_root(app), mail_mod.SECRET)
        elif password is not KEEP:
            secrets_mod.set_key(_root(app), mail_mod.SECRET, password)
        return await reload.commit(app, {"email": clean})


async def vault(app, values: Any, secret: Any = KEEP, if_match: str | None = None) -> dict:
    """`secret` (the client secret) as email's password. Only when the login changed or
    the vault is not unlocked does it log in again, locked."""
    if secret is not KEEP:
        _secret("client_secret", secret)
    async with write.lock():
        s = app.cfg.settings
        files.check_version("vault", s.vault, if_match)
        clean = vault_mod.validate(values)
        before = s.vault or {}
        relogin = (secret is not KEEP or app.vault.session is None
                   or any(clean.get(k) != before.get(k) for k in ("url", "client_id")))
        if secret is None:
            secrets_mod.delete_key(_root(app), vault_mod.CLIENT_SECRET)
        elif secret is not KEEP:
            secrets_mod.set_key(_root(app), vault_mod.CLIENT_SECRET, secret)
        out = await reload.commit(app, {"vault": clean})
    if relogin:
        await app.vault.connect()
    return out


def provider_clean(pid: str, body: Any) -> dict:
    """The provider as `provider` saves it: validated, a ComfyUI address alone given
    every shipped preset as a model."""
    if isinstance(body, dict) and body.get("kind") == "comfyui" and not body.get("models"):
        # the onboarding's address only: every shipped preset is a model of it
        from .comfyui import presets as presets_mod
        body = {**body, "models": {p.id: {"label": p.label, "caps": [p.role]}
                                   for kind in presets_mod.FOLDERS
                                   for p in presets_mod.of(kind).values()}}
    return validate_provider(pid, body, "provider")


async def provider(app, pid: str, body: Any, if_match: str | None = None) -> dict:
    """Create or replace one provider, models included; a role left empty that one of
    its models fits gets it (views.fill_roles)."""
    async with write.lock():
        s = app.cfg.settings
        files.check_version("provider", s.providers.get(pid), if_match)
        clean = provider_clean(pid, body)
        providers = copy.deepcopy(s.providers)
        providers[pid] = clean
        changes: dict[str, Any] = {"providers": providers}
        if filled := views.fill_roles(s, pid, clean):
            changes["roles"] = filled
        return await reload.commit(app, changes)


async def provider_key(app, pid: str, key: str | None, if_match: str | None = None) -> dict:
    """A provider's key: a string sets it, None removes it. The backends pick it up and
    the masking learns it. -> {"applied"}, as a commit answers."""
    async with write.lock():
        s = app.cfg.settings
        if pid not in s.providers:
            raise NotFound("provider")
        files.check_version("provider", s.providers[pid], if_match)
        if key is None:
            secrets_mod.delete_key(_root(app), pid)
        else:
            if not isinstance(key, str):
                raise Invalid("key", "The key must be text.")
            secrets_mod.set_key(_root(app), pid, key)
        applied = reload.apply(app)
    write.publish(app, ["secrets"])
    return {"applied": applied}


async def plugin_form(app, p: plugins.Plugin, values: Any, secrets: Any,
                      if_match: str | None = None) -> dict:
    """A plugin's whole `values` and the `secrets` given; one with [servers] starts its
    runner again, so its servers run with what was saved."""
    s = app.cfg.settings
    async with write.lock():
        files.check_version(p.manifest["label"], form_version(s, p), if_match)
        clean = form.validate(p.id, p.form["fields"], values)
        checked = form.check_secrets(p.id, p.form["fields"], secrets)
        if checked and s.secrets_root is None:
            raise Invalid(f"{p.id}.secrets", "This instance keeps no secrets.")
        with form.written(p.id, s.secrets_root, checked):   # a failed save puts them back
            out = await reload.commit(app, {p.id: clean})
    if p.manifest.get("servers"):
        await views.apply_runners(app, {p.id})
    return out


def form_version(s, p: plugins.Plugin) -> dict[str, Any]:
    """A plugin form's version input: its saved values and each secret's state."""
    return {"values": s.plugins.get(p.id, {}),
            "secrets": form.secret_states(p.id, p.form["fields"], s.secrets_root)}


async def context(app, values: dict[str, Any], if_match: str | None = None) -> dict:
    """The context shares (var/settings/context.toml, turns/loads.py): the whole set, a
    key left out its default. The file keeps only the keys that differ from the
    defaults, so a later default reaches every key left at it. -> {"applied"}, as a
    commit answers."""
    from .turns import loads
    for k, v in values.items():
        if (why := loads.share_problem(k, v)) is not None:
            raise Invalid(f"context.{k}", why)
    async with write.lock():
        files.check_version("context", app.cfg.context, if_match)
        files.write_toml(seed.context_path(files.settings_dir(app.cfg)),
                         {k: v for k, v in values.items() if v != loads.DEFAULTS[k]})
        applied = reload.apply(app)
    write.publish(app, ["context"])
    return {"applied": applied}
