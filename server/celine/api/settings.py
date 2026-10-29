"""Settings API: providers, models, keys, task models (roles), tool defaults, general
settings, features, services, email, the vault, and the per-conversation chat model. The agent's persona, voice and
portrait are its own (api/her.py).

Rules every route keeps:
- Stateless. Every PUT takes the whole object and is idempotent; the same PUT twice
  leaves the same files. DELETE of something already gone answers 200.
- Versioned. GET returns `version` (and an ETag header). A PUT/DELETE with
  `If-Match: <version>` that no longer matches answers 412 `version_conflict`.
- Validation errors are 400 `{"error": {"code": "bad_request", "message": "<one plain
  sentence>", "detail": {"field": "<path>"}}}`.
- Keys are write-only: nothing returns a key or a part of one.
- Every write is reloaded in place and `{"type": "settings.changed", "what": [...]}`
  goes out on the global feed (WS /ws/events), so every open page shows it at once. Provider and model
  state (reachable, loaded, failed) goes out as `models.state` (backends/router.py).
"""
from __future__ import annotations

import asyncio
import copy
from urllib.parse import urlsplit

from fastapi import APIRouter, Header, Request, Response

from .. import reload
from ..config import build as config_build
from ..errors import CelineError, NotFound
from ..settings import features as features_mod, files, general as general_mod, kinds
from ..settings import mail as mail_mod, services as services_mod
from ..settings import saves, vault as vault_mod, views, write
from ..settings import secrets as secrets_mod
from ..settings.files import Invalid, version
from ..settings.registry import ROLES, key_state, validate_model
from .deps import cfg_of, etag, json_body, person_only, services_of, settings_of

router = APIRouter(prefix="/api")

# ---- providers -----------------------------------------------------------------------

@router.get("/providers")
async def list_providers(request: Request, response: Response) -> dict:
    s = settings_of(request)
    router = views.model_router(services_of(request))
    for pid in s.providers:             # a list the user opens shows the state now
        if not s.key_missing(pid):
            await router.probe(pid)
    return {"providers": [views.provider(s, pid, router) for pid in s.providers],
            "kinds": views.kinds_view(),
            "version": etag(response, version(s.providers)),
            "saved": bool(s.saved.get("providers"))}


@router.get("/providers/{pid}")
async def get_provider(request: Request, response: Response, pid: str) -> dict:
    s = settings_of(request)
    if pid not in s.providers:
        raise NotFound("provider")
    router = views.model_router(services_of(request))
    if not s.key_missing(pid):
        await router.probe(pid)
    view = views.provider(s, pid, router)
    etag(response, view["version"])
    return view


@router.put("/providers/{pid}")
async def put_provider(request: Request, response: Response, pid: str,
                       if_match: str | None = Header(default=None)) -> dict:
    """Create or replace one provider, models included. Removing a model a role uses
    is refused (400)."""
    out = await saves.provider(services_of(request), pid, await json_body(request),
                               if_match)
    view = views.provider(settings_of(request), pid, views.model_router(services_of(request)))
    etag(response, view["version"])
    return {**view, **out}


@router.delete("/providers/{pid}")
async def delete_provider(request: Request, pid: str,
                          if_match: str | None = Header(default=None)) -> dict:
    async with write.lock():
        s = settings_of(request)
        if pid not in s.providers:
            return {"deleted": False}
        files.check_version("provider", s.providers.get(pid), if_match)
        used = views.used_by(s, pid)
        if used:
            roles = sorted({r for rs in used.values() for r in rs})
            raise CelineError("in_use", f"{s.providers[pid].get('label', pid)} is used for "
                              f"{', '.join(ROLES[r]['label'] for r in roles)}. "
                              f"First remove its models from those roles.", 409,
                              {"roles": ",".join(roles)})
        providers = {k: v for k, v in s.providers.items() if k != pid}
        out = await reload.commit(services_of(request), {"providers": providers})
        root = secrets_mod.secrets_dir(cfg_of(request).data_dir)
        secrets_mod.delete_key(root, pid)
        return {"deleted": True, **out}


@router.post("/providers/{pid}/test")
async def test_saved_provider(request: Request, pid: str) -> dict:
    """"Check again": the same probe the app makes before it uses a provider, asked
    now. Models that could not be loaded get another chance, and saved models pick up
    capabilities, context and thinking levels the server now reports. Returns the
    provider's view with its state."""
    s = settings_of(request)
    if pid not in s.providers:
        raise NotFound("provider")
    router = views.model_router(services_of(request))
    if not s.key_missing(pid):
        await router.probe(pid, max_age=0)
        await router.discover(pid, force=True)
        found = await views.discovered(s, pid, router)
        if found:
            async with write.lock():
                providers = views.merge_discovered(settings_of(request), pid, found)
                if providers:
                    await reload.commit(services_of(request), {"providers": providers})
        s = settings_of(request)
    return views.provider(s, pid, router)


@router.post("/provider-checks")
async def test_new_provider(request: Request) -> dict:
    """"Check and add": test an address (and key) before saving anything. Without a
    `kind` the server itself says what it is (`kinds.detect`); the answer names the
    kind, or `kind: null` with one sentence when nothing answered as a known kind."""
    body = await json_body(request, allow=("kind", "base_url", "key"))
    kind = body.get("kind") or None
    if kind is not None and kind not in kinds.KINDS:
        raise Invalid("kind", "The kind must be one of " + ", ".join(kinds.KINDS) + ".")
    raw = str(body.get("base_url") or "")
    # as typed for detection (it tries /v1 itself), the kind's one saved form after
    url = kinds.normalize_url(kind, raw) if kind else kinds.address(raw)
    if not kinds.valid_url(url):
        raise Invalid("base_url", "That is not an address. Try http://192.168.1.20:11434 "
                                  "or similar.")
    key = body.get("key")
    key = key.strip() if isinstance(key, str) else None
    if kind is None:
        kind, answered, url = await kinds.detect(url, key or None)
        if kind is None:
            if answered:
                sentence = (f"The app cannot detect the provider at "
                            f"{urlsplit(url).netloc}.")
            else:                       # the check says why: refused, no answer, ...
                sentence = (await kinds.check("openai", url, key or None,
                                              key_required=False))["sentence"]
            return {"ok": False, "level": "needs_you", "kind": None, "base_url": url,
                    "sentence": f"{sentence} Pick the type if you know it."}
        url = kinds.normalize_url(kind, url)
    out = await kinds.check(kind, url, key or None,
                            key_required=kinds.KINDS[kind]["key"] == "required")
    return {**out, "kind": kind, "base_url": url}


@router.get("/providers/{pid}/available-models")
async def available_models(request: Request, pid: str, refresh: bool = False,
                           q: str = "") -> dict:
    """What the provider itself offers (discovery), with `added` for models already
    chosen. Cached a minute per provider; refresh=true asks again."""
    s = settings_of(request)
    p = s.providers.get(pid)
    if p is None:
        raise NotFound("provider")
    from ..settings.registry import provider_key
    key = provider_key(pid, s.secrets_root)
    try:
        found = await kinds.discover(p["kind"], p["base_url"], key, refresh=refresh)
    except Exception as e:
        check = await kinds.check(p["kind"], p["base_url"], key, key_required=False)
        raise CelineError("unreachable", check.get("sentence") or
                          f"The app could not ask {p.get('label', pid)}: "
                          f"{type(e).__name__}.",
                          502) from None
    have = set(p.get("models") or {})
    ql = q.strip().lower()
    out = [{**m, "added": m["id"] in have} for m in found
           if not ql or ql in m["id"].lower() or ql in str(m.get("name", "")).lower()]
    return {"models": out, "total": len(found)}


@router.put("/providers/{pid}/models/{model:path}")
async def put_model(request: Request, response: Response, pid: str, model: str,
                    if_match: str | None = Header(default=None)) -> dict:
    async with write.lock():
        s = settings_of(request)
        p = s.providers.get(pid)
        if p is None:
            raise NotFound("provider")
        files.check_version("model", (p.get("models") or {}).get(model), if_match)
        clean = validate_model(p["kind"], model, await json_body(request), "model")
        providers = copy.deepcopy(s.providers)
        providers[pid].setdefault("models", {})[model] = clean
        out = await reload.commit(services_of(request), {"providers": providers})
        m = settings_of(request).providers[pid]["models"][model]
        etag(response, version(m))
        return {"ref": f"{pid}/{model}", "model": m, "version": version(m), **out}


@router.delete("/providers/{pid}/models/{model:path}")
async def delete_model(request: Request, pid: str, model: str,
                       if_match: str | None = Header(default=None)) -> dict:
    async with write.lock():
        s = settings_of(request)
        p = s.providers.get(pid)
        if p is None or model not in (p.get("models") or {}):
            return {"deleted": False}
        files.check_version("model", p["models"][model], if_match)
        used = views.used_by(s, pid, model).get(model)
        if used:
            raise CelineError("in_use", f"{p['models'][model].get('label', model)} is used "
                              f"for {', '.join(ROLES[r]['label'] for r in used)}. "
                              f"First remove the model from those roles.", 409,
                              {"roles": ",".join(used)})
        providers = copy.deepcopy(s.providers)
        del providers[pid]["models"][model]
        out = await reload.commit(services_of(request), {"providers": providers})
        return {"deleted": True, **out}


# ---- keys (write-only) ---------------------------------------------------------------

@router.get("/secrets")
async def list_secrets(request: Request) -> dict:
    s = settings_of(request)
    return {"secrets": {pid: key_state(pid, p, s.secrets_root)
                        for pid, p in s.providers.items()}}


@router.put("/secrets/{pid}")
async def put_secret(request: Request, pid: str) -> dict:
    """{"key": "..."}. Stores it; the answer only says it is set."""
    body = await json_body(request, need=("key",), allow=("key",))
    await saves.provider_key(services_of(request), pid, body["key"])
    root = secrets_mod.secrets_dir(cfg_of(request).data_dir)
    return key_state(pid, settings_of(request).providers[pid], root)


@router.delete("/secrets/{pid}")
async def delete_secret(request: Request, pid: str) -> dict:
    root = secrets_mod.secrets_dir(cfg_of(request).data_dir)
    secrets_mod.delete_key(root, pid)
    reload.apply(request.app.state.services)
    write.publish(services_of(request), ["secrets"])
    s = settings_of(request)
    p = s.providers.get(pid)
    return key_state(pid, p, root) if p else {"state": "not set", "source": None,
                                              "set_at": None}


# ---- roles ---------------------------------------------------------------------------

@router.get("/roles")
async def get_roles(request: Request, response: Response) -> dict:
    s = settings_of(request)
    return {"roles": views.roles(s), "version": etag(response, version(s.roles)),
            "saved": bool(s.saved.get("roles"))}


@router.put("/roles")
async def put_roles(request: Request, response: Response,
                    if_match: str | None = Header(default=None)) -> dict:
    """Every task at once: {"roles": {"chat": {"chain": [...]}, ...}}. A task left
    out is emptied (off)."""
    body = await json_body(request, need=("roles",), allow=("roles",))
    if not isinstance(body["roles"], dict):
        raise Invalid("roles", "Roles are an object keyed by role.")
    async with write.lock():
        s = settings_of(request)
        files.check_version("roles", s.roles, if_match)
        out = await reload.commit(services_of(request), {"roles": body["roles"]})
        s = settings_of(request)
        return {"roles": views.roles(s), "version": etag(response, version(s.roles)), **out}


@router.put("/roles/{role}")
async def put_role(request: Request, response: Response, role: str,
                   if_match: str | None = Header(default=None)) -> dict:
    """One task: {"chain": ["provider/model", ...]}. An empty chain turns it off."""
    body = await json_body(request, need=("chain",), allow=("chain",))
    out = await saves.role(services_of(request), role, body, if_match)
    view = views.roles(settings_of(request))[role]
    etag(response, view["version"])
    return {"role": role, **view, **out}


# ---- tools ---------------------------------------------------------------------------

async def _check_web(request: Request) -> None:
    reg = request.app.state.services.registry
    web = reg.skills.get("web") if reg is not None else None
    if web is not None and hasattr(web, "check"):
        await web.check()


@router.get("/tool-defaults")
async def get_tools(request: Request, response: Response) -> dict:
    await _check_web(request)
    view = views.tools(services_of(request))
    etag(response, view["version"])
    return view


@router.put("/tool-defaults")
async def put_tools(request: Request, response: Response,
                    if_match: str | None = Header(default=None)) -> dict:
    """Every default at once: {"defaults": {"chat_archive__search": "off",
    "shell": "on"}}."""
    body = await json_body(request, need=("defaults",), allow=("defaults",))
    async with write.lock():
        s = settings_of(request)
        files.check_version("tools", s.tools, if_match)
        out = await reload.commit(services_of(request), {"tools": body["defaults"]})
        view = views.tools(services_of(request))
        etag(response, view["version"])
        return {**view, **out}


@router.put("/tool-defaults/{target}")
async def put_tool_default(request: Request, response: Response, target: str,
                           if_match: str | None = Header(default=None)) -> dict:
    """One target: a skill ("chat_archive") or a tool ("chat_archive__search"). null
    removes the Settings entry, so the tool follows its skill / config again. Setting a skill
    clears the tool entries under it (the skill's switch is the bulk verb)."""
    body = await json_body(request, need=("default",), allow=("default",))
    if body["default"] not in ("on", "off", None):
        raise Invalid("default", "A default is \"on\", \"off\" or null.")
    out = await saves.tools(services_of(request), {target: body["default"]}, if_match)
    view = views.tools(services_of(request))
    etag(response, view["version"])
    return {**view, **out}


# ---- general -------------------------------------------------------------------------

@router.get("/settings")
async def get_general(request: Request, response: Response) -> dict:
    s = settings_of(request)
    fields = general_mod.fields()
    values = {f["key"]: general_mod.value(s.general, f["key"]) for f in fields}
    return {"fields": fields, "general": values,
            "version": etag(response, version(s.general)),
            "saved": bool(s.saved.get("general"))}


@router.put("/settings")
async def put_general(request: Request, response: Response,
                      if_match: str | None = Header(default=None)) -> dict:
    """The whole object: {"general": {...}}. A key left out goes back to its default."""
    body = await json_body(request, need=("general",), allow=("general",))
    out = await saves.general(services_of(request), body["general"], if_match)
    return {**(await get_general(request, response)), **out}


# ---- features ------------------------------------------------------------------------

@router.get("/features")
async def get_features(request: Request, response: Response) -> dict:
    view = await views.features(services_of(request))
    etag(response, view["version"])
    return view


@router.get("/features/{id}/probe")
async def probe_feature(request: Request, id: str) -> dict:
    """"Check again": one feature's state now, no cache."""
    if id not in features_mod.ids():
        raise NotFound(f"There is no feature '{id}'.")
    services = request.app.state.services
    return await features_mod.probe(services.cfg, services, id)


@router.put("/features")
async def put_features(request: Request, response: Response,
                       if_match: str | None = Header(default=None)) -> dict:
    """The whole object: {"features": {id: bool}, "done"?: true}. An id left out is on. A
    feature whose service is missing may be saved on (the user starts it later). `done`
    marks the onboarding finished and stays. What runs for a feature starts or stops at
    once (core/runners.py)."""
    body = await json_body(request, need=("features",), allow=("features", "done"))
    if not isinstance(body.get("done", False), bool):
        raise Invalid("done", "This must be true or false.")
    out, probes = await saves.features(services_of(request), body["features"],
                                       body.get("done", False), if_match)
    view = await views.features(services_of(request), probes)
    etag(response, view["version"])
    return {**view, **out}


# ---- services ------------------------------------------------------------------------
# The addresses of the shellbox, the browser and SearXNG, and the hash of each box's key
# (settings/services.py, celine/keys.py): what goes into a box's key.hash file.

@router.get("/services")
async def get_services(request: Request, response: Response) -> dict:
    view = views.services_view(services_of(request))
    etag(response, view["version"])
    return view


@router.put("/services")
async def put_services(request: Request, response: Response,
                       if_match: str | None = Header(default=None)) -> dict:
    """{"services": {shellbox: {url}, browser: {url}, searxng: {url}}}. The whole object: a
    url left out is empty (searxng: its default). A service whose address changed starts
    again while its feature is on."""
    body = await json_body(request, need=("services",), allow=("services",))
    out = await saves.addresses(services_of(request), body["services"], if_match)
    view = views.services_view(services_of(request))
    etag(response, view["version"])
    return {**view, **out}


@router.post("/services/{id}/test")
async def test_service(request: Request, id: str) -> dict:
    """The feature's probe, fresh, against the saved address: {state, detail}."""
    if id not in services_mod.IDS:
        raise NotFound(f"There is no service '{id}'.")
    feature = "web" if id == "searxng" else id
    services = request.app.state.services
    return await features_mod.probe(services.cfg, services, feature)


# ---- email ---------------------------------------------------------------------------

@router.get("/email")
async def get_email(request: Request, response: Response) -> dict:
    view = views.email(cfg_of(request))
    etag(response, view["version"])
    return view


@router.put("/email")
async def put_email(request: Request, response: Response,
                    if_match: str | None = Header(default=None)) -> dict:
    """{"email": {host, port, tls, user}, "password"?: "..." | null}. The whole object:
    a key left out goes back to its default. `password` left out keeps the saved one,
    null removes it. The answer says only whether a password is set."""
    body = await json_body(request, need=("email",), allow=("email", "password"))
    out = await saves.email(services_of(request), body["email"],
                            body.get("password", saves.KEEP), if_match)
    view = views.email(cfg_of(request))
    etag(response, view["version"])
    return {**view, **out}


@router.post("/email/test")
async def test_email(request: Request) -> dict:
    """Logs in with the saved account: {ok, sentence, folders: [{name, messages,
    unread}]}. A failure is one sentence."""
    from ..skills.mail import MailError, folders
    a = mail_mod.account(cfg_of(request))
    if a is None:
        return {"ok": False, "folders": [],
                "sentence": "Add the IMAP server, the user name and the password first."}
    try:
        found = await asyncio.to_thread(folders, a)
    except MailError as e:
        return {"ok": False, "folders": [], "sentence": str(e)}
    unread = sum(f["unread"] or 0 for f in found)
    return {"ok": True, "folders": found,
            "sentence": f"Logged in as {a.user}: {len(found)} folders, {unread} unread."}


# ---- vault ---------------------------------------------------------------------------
# The master password is stored nowhere: the user types it to unlock (POST
# /vault/unlock) and the vault keeps only the session key, in memory. Unlocking,
# locking, the vault's settings and the taps (celine/approvals.py) take a logged-in
# session: a named token, the kind a script or its shellbox can hold, is refused.

@router.get("/vault")
async def get_vault(request: Request, response: Response) -> dict:
    view = views.vault(services_of(request))
    etag(response, view["version"])
    return view


@router.put("/vault")
async def put_vault(request: Request, response: Response,
                    if_match: str | None = Header(default=None)) -> dict:
    """{"vault": {url, client_id}, "client_secret"?: "..." | null}. The whole object, as
    the email form's: a secret left out keeps the saved one, null removes it. Only when
    the login changed (server, client id, a secret given) or the vault is not unlocked
    does it log in again, locked; an unlocked vault stays so when only the collection
    changes (it is looked up on every call).
    The answer says how (status) and whether the secret is set."""
    person_only(request)
    body = await json_body(request, need=("vault",), allow=("vault", "client_secret"))
    out = await saves.vault(services_of(request), body["vault"],
                            body.get("client_secret", saves.KEEP), if_match)
    view = views.vault(services_of(request))
    etag(response, view["version"])
    return {**view, **out}


@router.post("/vault/unlock")
async def unlock_vault(request: Request) -> dict:
    """{"master_password": "..."}: to `bw unlock` once, kept nowhere. -> the view."""
    person_only(request)
    body = await json_body(request, need=("master_password",), allow=("master_password",))
    master = body.pop("master_password")
    if not isinstance(master, str) or not master:
        raise Invalid("master_password", "This must be text.")
    await request.app.state.services.vault.unlock(master)
    del master
    return views.vault(services_of(request))


@router.post("/vault/lock")
async def lock_vault(request: Request) -> dict:
    """The session key gone; its approvals of this unlock too. -> the view."""
    person_only(request)
    await request.app.state.services.vault.lock_now()
    return views.vault(services_of(request))


@router.get("/status")
async def config_status(request: Request) -> dict:
    """Configuration problems (a broken file, an unknown key, a missing model), for
    Settings, General ("To fix"), plus your name and `owner`: it, as every page draws
    it (her.view: name, pronouns, color, portrait, named). `hosts`: each provider's
    host as the gateway's ping sees it ({state: up | down | null, since}); `hold`:
    {state: "paused", since} while it is paused, else null (event `her.hold`); `build`:
    {commit, built, source} of this build (config.build); `vault_locked`: set up and locked."""
    from .. import her
    services = request.app.state.services
    cfg, router = services.cfg, getattr(services.llm, "router", None)
    return {"owner": her.view(cfg),
            "user": cfg.user,
            "features": features_mod.effective(cfg.settings.features),
            "setup": features_mod.done(cfg.settings.features),
            "catalog": [{"id": f.id, "label": f.label, "form": f.form,
                         "about": f.about, "off_when": f.off_when,
                         "places": list(f.places), "pages": list(f.pages)}
                        for f in features_mod.catalog()],
            "problems": [{"file": p.file, "message": p.message, "fatal": p.fatal}
                         for p in cfg.problems],
            "hosts": router.hosts() if router is not None else {},
            "hold": router.hold() if router is not None else None,
            "build": config_build(),
            "vault_locked": (services.vault is not None and services.vault.session is None
                             and vault_mod.account(cfg) is not None)}
