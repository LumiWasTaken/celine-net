"""She configures herself: `settings__read` shows every setting the user sees in
Settings as data (each target's fields, current values, allowed values, version; a
secret only as its state), and `settings__propose` changes one target. A proposal is
checked at once by the validators a Settings save runs (settings/write.check), waits for
the user's approval (celine/approvals.py, kind `settings`: each field's before and after,
and her why), and once approved is saved by the same function as the Settings screen
(celine/saves.py) against the version it was proposed on: a target that changed
meanwhile is not overwritten.

A secret is write-only: she names a file in her shellbox that holds it, and the value
goes from there into var/secrets without passing her context, the request, an event or
a log."""
from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any

from .. import plugins
from ..settings import features as features_mod, form, general as general_mod
from .. import saves
from ..settings import kinds, mail as mail_mod, secrets as secrets_mod
from ..settings import services as services_mod, vault as vault_mod, write
from ..errors import CelineError
from ..settings.files import Conflict, Invalid, version
from ..settings.registry import ROLES
from . import ToolContext, ToolResult, described, guide, tool

NOBODY = "Nothing was changed: nobody is here to approve it."
SHOWN = 120         # characters of one value in the request
SECRET_READ = 64 * 1024     # bytes of a secret's file read at most
PROVIDER = {"kind": "Type", "label": "Name", "base_url": "Address", "options": "Options",
            "models": "Models"}


@dataclass
class Target:
    """One thing Settings saves at once. `fields`: form.py records, each with `value`
    (a secret: `state` instead); `merge(changes)` -> the new values as the save takes
    them; `check(new)` raises Invalid as the save would; `save(app, new, secrets,
    version)` is the Settings screen's save."""
    id: str
    label: str
    version: str
    fields: list[dict[str, Any]]
    merge: Callable[[dict[str, Any]], Any]
    check: Callable[[Any], None]
    save: Callable[..., Awaitable[Any]]
    secrets: dict[str, str] = field(default_factory=dict)   # key -> its file's name

    def view(self) -> dict[str, Any]:
        return {"target": self.id, "label": self.label, "version": self.version,
                "fields": self.fields}


def _saved_merge(saved: dict[str, Any]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Changes over a saved section: null puts a key back to its default."""
    def merge(changes):
        out = dict(saved)
        for k, v in changes.items():
            if v is None:
                out.pop(k, None)
            else:
                out[k] = v
        return out
    return merge


def _check(cfg, section: str) -> Callable[[Any], None]:
    def check(new):
        write.check(cfg, {section: new})
    return check


def _form_fields(fields: list[dict], values: dict, states: dict) -> list[dict]:
    out = []
    for f in fields:
        if f["type"] == "copy":
            continue
        if f["type"] == "secret":
            out.append({**f, "state": states.get(f["key"], {}).get("state", "not set")})
        else:
            out.append({**f, "value": values.get(f["key"], f.get("default"))})
    return out


def targets(app) -> list[str]:
    s = app.cfg.settings
    return ["features", "general", "roles", "tools", "services", "email", "vault",
            "context", *(f"providers.{pid}" for pid in s.providers),
            *(f"forms.{p.id}" for p in plugins.with_form())]


def target_of(app, tid: str) -> Target | str:
    """The target `tid` as it is now, or the sentence why there is none."""
    cfg = app.cfg
    s = cfg.settings
    root = secrets_mod.secrets_dir(cfg.data_dir)
    kind, _, rest = tid.partition(".")
    if tid == "general":
        fields = general_mod.fields()
        values = {f["key"]: general_mod.value(s.general, f["key"]) for f in fields}
        return Target(tid, "General", version(s.general), _form_fields(fields, values, {}),
                      _saved_merge(s.general), _check(cfg, "general"),
                      lambda a, new, _, v: saves.general(a, new, v))
    if tid == "features":
        now = features_mod.effective(s.features)
        fields = [{"key": f.id, "label": f.label, "type": "bool", "help": f.about,
                   "value": now[f.id]} for f in features_mod.catalog()]

        async def save_features(a, new, _, v):
            return (await saves.features(a, new, False, v))[0]
        return Target(tid, "Features", version(s.features), fields,
                      lambda ch: {**now, **ch}, _check(cfg, "features"), save_features)
    if tid == "roles":
        refs = [(f"{pid}/{m}", set(spec.get("caps") or ())) for pid, p in s.providers.items()
                for m, spec in (p.get("models") or {}).items()]
        fields = [{"key": r, "label": spec["label"], "type": "list", "needs": spec["caps"],
                   "choices": [ref for ref, caps in refs if set(spec["caps"]) <= caps],
                   "value": (s.roles.get(r) or {}).get("chain") or []}
                  for r, spec in ROLES.items()]

        def merge_roles(ch):
            out = copy.deepcopy(s.roles)
            for r, chain in ch.items():
                out[r] = {"chain": chain or []}
            return out
        return Target(tid, "Models", version(s.roles), fields, merge_roles, _check(cfg, "roles"),
                      lambda a, new, _, v: saves.roles(a, new, v))
    if tid == "tools":
        from ..settings import views
        fields = []
        for sk in views.tools(app)["skills"]:
            fields.append({"key": sk["name"], "label": sk["label"], "value": sk["default"]})
            fields += [{"key": t["wire_name"], "label": t["label"], "value": t["default"]}
                       for t in sk["tools"]]
        for f in fields:
            f.update(type="choice", choices=[["on", "On"], ["off", "Off"]])

        def check_tools(ch):
            new = s.tools
            for t, d in ch.items():
                new = saves.tool_set(new, t, d)
            write.check(cfg, {"tools": new})
        return Target(tid, "Tools", version(s.tools), fields, dict, check_tools,
                      lambda a, new, _, v: saves.tools(a, new, v))
    if tid == "services":
        fields = [{**services_mod.FIELDS[sid][0], "key": sid,
                   "label": features_mod.get("web" if sid == "searxng" else sid).label,
                   "value": services_mod.value(s.services, sid, "url")}
                  for sid in services_mod.IDS]

        def merge_services(ch):
            out = copy.deepcopy(s.services)
            for sid, url in ch.items():
                out[sid] = {"url": url} if url is not None else {}
            return out
        return Target(tid, "Services", version(s.services), fields, merge_services,
                      _check(cfg, "services"), lambda a, new, _, v: saves.addresses(a, new, v))
    if tid == "email":
        values = {f["key"]: mail_mod.value(s.email, f["key"]) for f in mail_mod.FIELDS}
        fields = _form_fields([*mail_mod.FIELDS, {"key": "password", "label": "Password",
                                                  "type": "secret"}], values,
                              {"password": secrets_mod.state(root, mail_mod.SECRET)})
        return Target(tid, "Email", version(s.email), fields, _saved_merge(s.email),
                      _check(cfg, "email"),
                      lambda a, new, sec, v: saves.email(
                          a, new, sec.get("password", saves.KEEP), v),
                      {"password": mail_mod.SECRET})
    if tid == "vault":
        values = {f["key"]: vault_mod.value(s.vault, f["key"]) for f in vault_mod.FIELDS}
        fields = _form_fields([*vault_mod.FIELDS, {"key": "client_secret",
                                                   "label": "Client secret",
                                                   "type": "secret"}], values,
                              {"client_secret": secrets_mod.state(
                                  root, vault_mod.CLIENT_SECRET)})
        return Target(tid, "Vault", version(s.vault), fields, _saved_merge(s.vault),
                      _check(cfg, "vault"),
                      lambda a, new, sec, v: saves.vault(
                          a, new, sec.get("client_secret", saves.KEEP), v),
                      {"client_secret": vault_mod.CLIENT_SECRET})
    if tid == "context":
        from ..turns import loads
        fields = [{"key": k, "label": k, "type": "number", "default": d,
                   "value": cfg.context.get(k, d)} for k, d in loads.DEFAULTS.items()]

        def check_context(new):
            for k, v in new.items():
                if (why := loads.share_problem(k, v)) is not None:
                    raise Invalid(f"context.{k}", why)
        return Target(tid, "Context", version(cfg.context), fields,
                      lambda ch: {**cfg.context, **{k: loads.DEFAULTS[k] if v is None else v
                                                    for k, v in ch.items()}},
                      check_context, lambda a, new, _, v: saves.context(a, new, v))
    if kind == "providers" and rest:
        pid, p = rest, s.providers.get(rest) or {}
        fields = [{"key": k, "label": label, "value": p.get(k)} for k, label in
                  PROVIDER.items()]
        fields[0].update(type="choice", choices=[[k, k] for k in kinds.KINDS])
        fields.append({"key": "key", "label": "Key", "type": "secret",
                       **({"state": secrets_mod.state(root, pid)["state"]} if p else
                          {"state": "not set"})})

        def merge_provider(ch):
            out = copy.deepcopy(p)
            for k, v in ch.items():
                if v is None:
                    out.pop(k, None)
                else:
                    out[k] = v
            return out

        def check_provider(new):
            write.check(cfg, {"providers": {**s.providers, pid: new}})

        async def save_provider(a, new, sec, v):
            if new != p:
                await saves.provider(a, pid, new, v)
                v = None        # the same approval: the key follows the new version
            if "key" in sec:
                await saves.provider_key(a, pid, sec["key"], v)
        return Target(tid, f"Models, {p.get('label') or pid}", version(s.providers.get(pid)),
                      fields, merge_provider, check_provider, save_provider, {"key": pid})
    if kind == "forms" and (pl := plugins.get(rest)) is not None and pl.form is not None:
        saved = s.plugins.get(pl.id, {})
        fields = _form_fields(pl.form["fields"], form.values(pl.form["fields"], saved),
                              form.secret_states(pl.id, pl.form["fields"], s.secrets_root))
        return Target(tid, pl.manifest["label"], version(saves.form_version(s, pl)), fields,
                      _saved_merge(saved), _check(cfg, pl.id),
                      lambda a, new, sec, v: saves.plugin_form(a, pl, new, sec, v),
                      {f["key"]: form.secret_name(pl.id, f["key"])
                       for f in pl.form["fields"] if f["type"] == "secret"})
    return (f"There is no target '{tid}'. Targets: {', '.join(targets(app))}, or "
            f"providers.<new id> for a new provider.")


# ---- the request's words: each field's before and after -------------------------------

def shown(f: dict[str, Any], v: Any) -> str:
    """A value as the request shows it."""
    if isinstance(v, bool):
        text = "On" if v else "Off"
    elif v is None:
        text = "Default"
    elif v == "" or v == [] or v == {}:
        text = "Empty"
    elif f.get("type") == "choice":
        text = next((c[1] for c in f.get("choices") or () if c[0] == v), str(v))
    elif isinstance(v, list):
        text = ", ".join(map(str, v))
    elif isinstance(v, dict):
        text = json.dumps(v, ensure_ascii=False, separators=(", ", ": "))
    else:
        text = str(v)
    text = " ".join(text.split())
    return text if len(text) <= SHOWN else text[:SHOWN - 1].rstrip() + "…"


def _after(f: dict[str, Any], v: Any) -> Any:
    """A field's value after the change; null is its default."""
    if v is None and "default" in f:
        return f["default"]
    return v


async def _secret_from(cfg, path: Any, where: str, transport=None) -> str:
    """The text of the secret's file in its shellbox, one line. Invalid without its
    value in the sentence."""
    from .. import box_files
    if not isinstance(path, dict) or set(path) != {"file"} or \
            not isinstance(path["file"], str) or not path["file"].strip():
        raise Invalid(where, 'A secret comes from a file in your shellbox: '
                             '{"file": "~/path"}; null removes it.')
    data = b""
    try:
        async for chunk in box_files.stream(cfg, path["file"].strip(), transport=transport):
            data += chunk
            if len(data) > SECRET_READ:
                raise Invalid(where, f"{path['file']} is larger than a secret.")
    except (FileNotFoundError, RuntimeError) as e:
        raise Invalid(where, str(e)) from None
    except Exception as e:      # the box does not answer
        raise Invalid(where, f"The shellbox did not give {path['file']}: "
                             f"{type(e).__name__}.") from None
    try:
        text = data.decode().strip()
    except UnicodeDecodeError:
        raise Invalid(where, f"{path['file']} is not text.") from None
    if not text or len(text) > secrets_mod.MAX_KEY or any(c in text for c in "\r\n\0"):
        raise Invalid(where, f"{path['file']} does not hold one line of text.")
    return text


class Settings:
    name = "settings"
    guide = guide("settings")
    description = described()

    def __init__(self, cfg, transport=None):
        self.cfg = cfg
        self.app = None
        self.transport = transport      # tests: a fake shellbox

    @tool()
    async def read(
        self, ctx: ToolContext,
        target: Annotated[str, "One target, like \"general\" or \"providers.main\"; empty "
                               "lists the targets."] = "",
    ) -> ToolResult:
        """Read the settings {{username}} sees in Settings, as data. With no target: the
        targets, each with its label and version. With a target: its fields, each with
        its key, label, type, allowed values (choices, min, max), default and current
        value; a secret only as its state, set or not set. Example:
        settings__read(target="general"). Returns: JSON."""
        if not target.strip():
            out = []
            for tid in targets(self.app):
                t = target_of(self.app, tid)
                out.append({"target": tid, "label": t.label, "version": t.version})
            return ToolResult(content=self.cfg.substitute(
                json.dumps({"targets": out}, ensure_ascii=False)))
        t = target_of(self.app, target.strip())
        if isinstance(t, str):
            return ToolResult(content=t, is_error=True)
        return ToolResult(content=self.cfg.substitute(
            json.dumps(t.view(), ensure_ascii=False, default=str)), display={"target": t.id})

    @tool(read_only=False, destructive=False)
    async def propose(
        self, ctx: ToolContext,
        target: Annotated[str, "The target, as settings__read names it."],
        changes: Annotated[dict, "{key: new value} for the fields to change; null puts "
                                 "one back to its default. A secret: {\"file\": \"~/path\"}, "
                                 "a file in your shellbox that holds it, or null to "
                                 "remove it."],
        why: Annotated[str, "Why, in one sentence, shown to {{username}} with the "
                            "change."],
    ) -> ToolResult:
        """Change settings of one target. Checked at once as Settings checks a save;
        then it waits until {{username}} approves it, and is saved as Settings saves it.
        Example: settings__propose(target="general", changes={"turn_wall_clock_seconds": 3600},
        why="Long research turns stop at the 30 minute limit."). Returns: what changed, or why
        nothing did (not valid, denied, no answer, or changed since you proposed it)."""
        t = target_of(self.app, target.strip())
        if isinstance(t, str):
            return ToolResult(content=t, is_error=True)
        if not isinstance(changes, dict) or not changes:
            return ToolResult(content="changes names the fields to change: {key: value}.",
                              is_error=True)
        by_key = {f["key"]: f for f in t.fields}
        unknown = [k for k in changes if k not in by_key]
        if unknown:
            return ToolResult(content=f"Not a setting of {t.id}: {', '.join(unknown)}. "
                                      f"settings__read(target=\"{t.id}\") lists them.",
                              is_error=True)
        values = {k: v for k, v in changes.items() if by_key[k].get("type") != "secret"}
        secrets: dict[str, str | None] = {}
        rows = []
        try:
            for k, v in changes.items():
                f = by_key[k]
                if f.get("type") == "secret":
                    secrets[k] = None if v is None else await _secret_from(
                        self.cfg, v, f"{t.id}.{k}", self.transport)
                    rows.append({"label": f["label"],
                                 "before": "Saved" if f["state"] == "set" else "Not set",
                                 "after": "Removed" if v is None else "New value"})
                    continue
                before, after = shown(f, f.get("value")), shown(f, _after(f, v))
                if before != after:
                    rows.append({"label": f["label"], "before": before, "after": after})
            new = t.merge(values)
            if values:
                t.check(new)
            if t.id.startswith("forms.") and secrets:
                form.check_secrets(t.id[6:], plugins.get(t.id[6:]).form["fields"], secrets)
        except Invalid as e:
            return ToolResult(content=f"{e.field}: {e.message}", is_error=True)
        if not rows:
            return ToolResult(content="Nothing changes: these are the values already.",
                              is_error=True)
        approvals = getattr(self.app, "approvals", None)
        if approvals is None:
            return ToolResult(content=NOBODY, is_error=True)
        action = f"change {t.label}"
        if no := await approvals.ask(action, [], ctx.conversation_id, why, action,
                                     kind="settings", call=ctx.tool_call_id or None,
                                     changes=rows):
            return ToolResult(content=no, is_error=True)
        try:
            await t.save(self.app, new, secrets, t.version)
        except Conflict:
            return ToolResult(content=f"{t.label} changed since you proposed it; nothing "
                                      f"was done. Read it again.", is_error=True)
        except CelineError as e:
            return ToolResult(content=e.message, is_error=True)
        said = "; ".join(f"{r['label']}: {r['before']} → {r['after']}" for r in rows)
        return ToolResult(content=f"Changed {t.label}. {said}.",
                          display={"target": t.id, "changes": rows})

