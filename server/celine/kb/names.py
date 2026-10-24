"""Display names, resolved at read time.

A person row carries every name a source ever gave them. The one shown is picked in
a fixed order, and `source` says which one won, so the UI can say "from your address
book" or "their own name" without guessing.
"""
from __future__ import annotations

from typing import Any, Mapping

import phonenumbers

ORDER = ("custom_name", "contact_name", "display_name", "push_name", "username")
SOURCE = {"custom_name": "custom", "contact_name": "contact", "display_name": "display",
          "push_name": "push", "username": "username"}


def format_phone(e164: str | None) -> str | None:
    """'+15551234567' -> '+1 555-123-4567'. Anything unparseable comes back as is."""
    if not e164 or sum(c.isdigit() for c in e164) < 6:
        return None
    try:
        num = phonenumbers.parse(e164 if e164.startswith("+") else f"+{e164}")
        if phonenumbers.is_possible_number(num):
            return phonenumbers.format_number(
                num, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except phonenumbers.NumberParseException:
        pass
    return e164


def unknown_label(key: str) -> str:
    digits = "".join(c for c in key.split("@")[0] if c.isdigit())
    return f"Unknown {digits[-4:]}" if digits else "Unknown"


def chat_label(key: str, kind: str) -> str:
    """A chat that has no name, by what its key says, never the key itself: a person's
    WhatsApp chat is their phone number, a WhatsApp group "group" and the last four
    digits of its id, another group or channel "Unnamed group" or "Unnamed channel", a
    broadcast list "Broadcast list"."""
    user, _, server = key.partition("@")
    if server == "s.whatsapp.net":
        return format_phone(user) or unknown_label(key)
    if server == "broadcast":
        return "Broadcast list"
    digits = "".join(c for c in user if c.isdigit())
    if server == "g.us" and digits:
        return f"group {digits[-4:]}"
    word = {"group": "Unnamed group", "channel": "Unnamed channel"}.get(kind)
    return word or unknown_label(key)


def resolve(row: Mapping[str, Any]) -> tuple[str, str]:
    """(name, source) for a people row."""
    for col in ORDER:
        v = row[col] if col in row.keys() else None
        if v and str(v).strip():
            return str(v).strip(), SOURCE[col]
    phone = format_phone(row["phone"])
    if phone:
        return phone, "phone"
    return unknown_label(row["key"]), "unknown"


def person(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    name, source = resolve(row)
    return {"id": row["id"], "name": name, "source": source,
            "phone": format_phone(row["phone"])}


# the names a person gave or was given, without the phone and key fallbacks
SQL_GIVEN = ("COALESCE(NULLIF(TRIM(p.custom_name), ''), NULLIF(TRIM(p.contact_name), ''), "
             "NULLIF(TRIM(p.display_name), ''), NULLIF(TRIM(p.push_name), ''), "
             "NULLIF(TRIM(p.username), ''))")

# a SQL expression that mirrors resolve(), for ORDER BY and LIKE filters.
# `p` is the people alias. Phone formatting is left to Python.
SQL_NAME = ("COALESCE(NULLIF(TRIM(p.custom_name), ''), NULLIF(TRIM(p.contact_name), ''), "
            "NULLIF(TRIM(p.display_name), ''), NULLIF(TRIM(p.push_name), ''), "
            "NULLIF(TRIM(p.username), ''), p.phone, p.key)")
