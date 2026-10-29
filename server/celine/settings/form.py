"""One form's fields, validated the same everywhere: General (settings/general.py) and
each plugin's `settings.toml` (celine/plugins.py), whose values live in
var/settings/<id>.toml.

A field is a dict: `key`, `label`, `type` (one of TYPES), `default`, and by type `choices`
([value, label] pairs), `min`/`max` (a number: a float default takes any number, an int one
whole numbers), `required`, `max_len`, `zone` (text)."""
from __future__ import annotations

from typing import Any

from .files import Invalid

TYPES = ("bool", "choice", "number", "text")


def check(path: str, f: dict[str, Any], v: Any) -> None:
    """One value against its field; raises Invalid with `path`."""
    if f["type"] == "bool":
        if not isinstance(v, bool):
            raise Invalid(path, "This must be true or false.")
    elif f["type"] == "choice":
        if v not in [c[0] for c in f["choices"]]:
            raise Invalid(path, "This must be one of " +
                          ", ".join(c[0] for c in f["choices"]) + ".")
    elif f["type"] == "number":
        if isinstance(f["default"], float):
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise Invalid(path, "This must be a number.")
        elif not isinstance(v, int) or isinstance(v, bool):
            raise Invalid(path, "This must be a whole number.")
        if v < f.get("min", v) or v > f.get("max", v):
            raise Invalid(path, f"This must be between {f['min']} and {f['max']}.")
    elif f["type"] == "text":
        if not isinstance(v, str):
            raise Invalid(path, "This must be text.")
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


def validate(section: str, fields: list[dict[str, Any]], data: Any) -> dict[str, Any]:
    """Only known keys, each checked; a key left out (or None) is its default."""
    if not isinstance(data, dict):
        raise Invalid(section, "These settings are an object.")
    by_key = {f["key"]: f for f in fields}
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k not in by_key:
            raise Invalid(f"{section}.{k}", f"'{k}' is not a setting here.")
        if v is None:
            continue
        check(f"{section}.{k}", by_key[k], v)
        out[k] = v
    return out


def values(fields: list[dict[str, Any]], saved: dict[str, Any]) -> dict[str, Any]:
    """Every field's value: as saved, else its default."""
    return {f["key"]: saved.get(f["key"], f.get("default")) for f in fields}
