"""The one form schema: the field record, `validate`, `view`, and the secrets of a form.
General (settings/general.py), email (mail.py), the vault (vault.py), the services
(services.py) and each plugin's `settings.toml` (celine/plugins.py) are validated here; a
module keeps only its extra checks after this pass. Forms are served by api/forms.py.

A field is a dict: `key`, `label`, `type` (one of TYPES), `default`, and by type:
- `text`: `required`, `max_len`, `zone` (an IANA time zone), `word` (stripped, no spaces
  inside, "" goes back to the default), and for the UI `long`, `mono`, `placeholder`;
- `number`, `duration`: `min`, `max` (a float default takes any number, an int one whole
  numbers), and for the UI `step`, `unit`;
- `choice`: `choices`, [value, label] pairs;
- `list`: a list of strings, one per line in the UI, blank lines dropped;
- `secret`: write-only, stored in <data>/secrets/<form>.<key>.key; the view says only
  {state, set_at};
- `copy`: a read-only string the form fills at view time, left out when it has none.
Common: `help`, `advanced`, `group`, `where: "config"` (read-only)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import secrets
from .files import Invalid

TYPES = ("bool", "choice", "number", "duration", "text", "list", "secret", "copy")
WRITE_ONLY = ("secret", "copy")     # never in `values`


def check(path: str, f: dict[str, Any], v: Any) -> Any:
    """One value against its field; -> the value as stored. Raises Invalid with `path`."""
    t = f["type"]
    if t == "bool":
        if not isinstance(v, bool):
            raise Invalid(path, "This must be true or false.")
    elif t == "choice":
        if v not in [c[0] for c in f["choices"]]:
            raise Invalid(path, "This must be one of " +
                          ", ".join(c[0] for c in f["choices"]) + ".")
    elif t in ("number", "duration"):
        if isinstance(f.get("default"), float):
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise Invalid(path, "This must be a number.")
        elif not isinstance(v, int) or isinstance(v, bool):
            raise Invalid(path, "This must be a whole number.")
        if v < f.get("min", v) or v > f.get("max", v):
            raise Invalid(path, f"This must be between {f['min']} and {f['max']}.")
    elif t == "text":
        if not isinstance(v, str):
            raise Invalid(path, "This must be text.")
        if f.get("word"):
            v = v.strip()
            if any(c.isspace() for c in v):
                raise Invalid(path, "This must be text without spaces.")
        if f.get("required") and not v.strip():
            raise Invalid(path, "This cannot be empty.")
        if len(v) > f.get("max_len", len(v)):
            raise Invalid(path, f"This is at most {f['max_len']} characters.")
        if f.get("zone"):
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(v)
            except Exception:
                raise Invalid(path, f"'{v}' is not a time zone. Try America/New_York.") \
                    from None
    elif t == "list":
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise Invalid(path, "This must be a list of text.")
        v = [x.strip() for x in v if x.strip()]
    return v


def validate(section: str, fields: list[dict[str, Any]], data: Any) -> dict[str, Any]:
    """Only known keys, each checked; a key left out (or None) is its default."""
    if not isinstance(data, dict):
        raise Invalid(section, "These settings are an object.")
    by_key = {f["key"]: f for f in fields}
    out: dict[str, Any] = {}
    for k, v in data.items():
        f = by_key.get(k)
        if f is None or f["type"] in WRITE_ONLY:
            raise Invalid(f"{section}.{k}", f"'{k}' is not a setting here.")
        if v is None:
            continue
        v = check(f"{section}.{k}", f, v)
        if v != "" or not f.get("word"):
            out[k] = v
    return out


def values(fields: list[dict[str, Any]], saved: dict[str, Any]) -> dict[str, Any]:
    """Every field's value: as saved, else its default."""
    return {f["key"]: saved.get(f["key"], f.get("default"))
            for f in fields if f["type"] not in WRITE_ONLY}


def check_toml(doc: dict[str, Any]) -> None:
    """A plugin's settings.toml as the loader reads it: [[fields]], an optional `note`,
    [[actions]] {id, label, input?, run = "module:coroutine"}; raises ValueError."""
    fields = doc.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ValueError("settings.toml needs [[fields]]")
    seen = set()
    for f in fields:
        if not isinstance(f, dict) or not isinstance(f.get("key"), str) \
                or not isinstance(f.get("label"), str):
            raise ValueError("every field needs a key and a label")
        if f["key"] in seen:
            raise ValueError(f"the field {f['key']!r} is there twice")
        seen.add(f["key"])
        if f.get("type") not in TYPES:
            raise ValueError(f"the field {f['key']!r} has no known type "
                             f"({', '.join(TYPES)})")
        if f["type"] == "choice" and not f.get("choices"):
            raise ValueError(f"the choice {f['key']!r} needs choices")
        if f["type"] not in WRITE_ONLY and "default" in f:
            try:
                check(f["key"], f, f["default"])
            except Invalid as e:
                raise ValueError(f"the default of {f['key']!r}: {e}") from None
    if not isinstance(doc.get("note", ""), str):
        raise ValueError("the note is one text")
    for a in doc.get("actions", []):
        if not isinstance(a, dict) or not all(isinstance(a.get(k), str)
                                              for k in ("id", "label", "run")):
            raise ValueError("every action needs an id, a label and a run")


def actions_view(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What a client sees of the actions: never the code they run."""
    return [{k: a[k] for k in ("id", "label", "input") if k in a} for a in actions]


def secret_name(section: str, key: str) -> str:
    """Its file in <data>/secrets: <section>.<key>.key."""
    return f"{section}.{key}"


def secret_states(section: str, fields: list[dict[str, Any]],
                  root: Path | None) -> dict[str, dict[str, Any]]:
    return {f["key"]: (secrets.state(root, secret_name(section, f["key"])) if root is not None
                       else {"state": "not set", "set_at": None})
            for f in fields if f["type"] == "secret"}


def view(section: str, fields: list[dict[str, Any]], saved: dict[str, Any],
         root: Path | None, copies: dict[str, str] | None = None) -> dict[str, Any]:
    """What a client gets: the fields (a `copy` without a value left out), the values,
    each secret's state, never a secret."""
    copies = copies or {}
    shown = [({**f, "value": copies[f["key"]]} if f["type"] == "copy" else f)
             for f in fields if f["type"] != "copy" or copies.get(f["key"])]
    return {"fields": shown, "values": values(fields, saved),
            "secrets": secret_states(section, fields, root)}


def check_secrets(section: str, fields: list[dict[str, Any]], data: Any) -> dict[str, Any]:
    """{key: "new" | None}: a string sets, None deletes; only secret fields."""
    if not isinstance(data, dict):
        raise Invalid(f"{section}.secrets", "The secrets are an object.")
    kinds = {f["key"]: f["type"] for f in fields}
    out: dict[str, Any] = {}
    for k, v in data.items():
        path = f"{section}.{k}"
        if kinds.get(k) != "secret":
            raise Invalid(path, f"'{k}' is not a secret here.")
        if v is not None:
            if not isinstance(v, str) or not v.strip():
                raise Invalid(path, "This must be text.")
            v = v.strip()
            if len(v) > secrets.MAX_KEY or any(c in v for c in "\r\n\0"):
                raise Invalid(path, "This is not one line of text.")
        out[k] = v
    return out


def write_secrets(section: str, root: Path, clean: dict[str, Any]) -> None:
    for k, v in clean.items():
        if v is None:
            secrets.delete_key(root, secret_name(section, k))
        else:
            secrets.set_key(root, secret_name(section, k), v)


def read_secret(cfg, section: str, key: str) -> str | None:
    """For the code that uses it. Never for an API response."""
    root = getattr(getattr(cfg, "settings", None), "secrets_root", None)
    return secrets.read_key(root, secret_name(section, key)) if root is not None else None
