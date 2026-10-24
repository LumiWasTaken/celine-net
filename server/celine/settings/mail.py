"""The email account Celine reads (skills/mail.py): var/settings/email.toml for the
server, var/secrets/email.key for the password (write-only, settings/secrets.py).

Each field has a schema the UI renders as a generic form, like the general settings.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .files import Invalid

SECRET = "email"            # var/secrets/email.key

FIELDS: list[dict[str, Any]] = [
    {"key": "host", "label": "IMAP server", "type": "text",
     "help": "Such as imap.example.com.", "default": ""},
    {"key": "port", "label": "Port", "type": "number", "min": 1, "max": 65535,
     "help": "993 with TLS, 143 with STARTTLS.", "default": 993},
    {"key": "tls", "label": "Encryption", "type": "choice",
     "choices": [["implicit", "TLS"], ["starttls", "STARTTLS"]], "default": "implicit"},
    {"key": "user", "label": "User name", "type": "text",
     "help": "Usually the email address.", "default": ""},
]
_BY_KEY = {f["key"]: f for f in FIELDS}


def value(email: dict[str, Any], key: str) -> Any:
    return email.get(key, _BY_KEY[key]["default"])


def validate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise Invalid("email", "The email settings are an object.")
    out: dict[str, Any] = {}
    for k, v in data.items():
        f = _BY_KEY.get(k)
        path = f"email.{k}"
        if f is None:
            raise Invalid(path, f"'{k}' is not an email setting.")
        if v is None or v == "":
            continue
        if f["type"] == "number":
            if not isinstance(v, int) or isinstance(v, bool) or not f["min"] <= v <= f["max"]:
                raise Invalid(path, f"This must be a whole number from {f['min']} to "
                                    f"{f['max']}.")
        elif f["type"] == "choice":
            if v not in [c[0] for c in f["choices"]]:
                raise Invalid(path, "This must be implicit or starttls.")
        elif not isinstance(v, str) or any(c.isspace() for c in v.strip()):
            raise Invalid(path, "This must be text without spaces.")
        out[k] = v.strip() if isinstance(v, str) else v
    return out


@dataclass(frozen=True)
class Account:
    host: str
    port: int
    tls: str            # implicit | starttls
    user: str
    password: str


def account(cfg) -> Account | None:
    """The account from Settings, or None while the server, user or password is
    missing."""
    from .secrets import read_key
    s = getattr(cfg, "settings", None)
    if s is None:
        return None
    e = s.email
    password = read_key(s.secrets_root, SECRET) if s.secrets_root is not None else None
    if not (e.get("host") and e.get("user") and password):
        return None
    return Account(e["host"], int(value(e, "port")), value(e, "tls"), e["user"], password)
