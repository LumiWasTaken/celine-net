"""The agent's vault on the user's Vaultwarden (server/celine/vault): var/settings/vault.toml for
the server and the API key's client id; the client secret is var/secrets/vault-api.key
(write-only, settings/secrets.py). The API key logs in and decrypts nothing. The master
password is stored nowhere: the user types it to unlock (vault.Vault.unlock).

Each field has a schema the UI renders as a generic form, like the email settings.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .files import Invalid

CLIENT_SECRET = "vault-api"     # var/secrets/vault-api.key

FIELDS: list[dict[str, Any]] = [
    {"key": "url", "label": "Server", "type": "text",
     "help": "The Vaultwarden address, such as https://vault.example.com.", "default": ""},
    {"key": "client_id", "label": "Client id", "type": "text",
     "help": "From the web vault: Settings, Security, Keys, View API key.", "default": ""},
    {"key": "collection", "label": "Collection", "type": "text",
     "help": "An organization collection the agent shares with you, by name (or by id when "
             "two have the same name): the agent's entries go there and it sees only that one. "
             "Required: without it the vault takes nothing.",
     "default": ""},
]
_BY_KEY = {f["key"]: f for f in FIELDS}


def value(vault: dict[str, Any], key: str) -> Any:
    return vault.get(key, _BY_KEY[key]["default"])


def validate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise Invalid("vault", "The vault settings are an object.")
    out: dict[str, Any] = {}
    for k, v in data.items():
        path = f"vault.{k}"
        if k not in _BY_KEY:
            raise Invalid(path, f"'{k}' is not a vault setting.")
        if v is None or v == "":
            continue
        if not isinstance(v, str) or any(c.isspace() for c in v.strip()):
            raise Invalid(path, "This must be text without spaces.")
        v = v.strip()
        if k == "url" and (urlsplit(v).scheme != "https" or not urlsplit(v).netloc):
            # bw takes no other: "Insecure URL not allowed"
            raise Invalid(path, "This must be an https address, such as "
                                "https://vault.example.com.")
        out[k] = v
    return out


@dataclass(frozen=True)
class Account:
    url: str
    client_id: str
    client_secret: str
    collection: str | None = None


def account(cfg) -> Account | None:
    """What logs in, or None while any of the three is missing."""
    from .secrets import read_key
    s = getattr(cfg, "settings", None)
    if s is None or s.secrets_root is None:
        return None
    v = s.vault
    secret = read_key(s.secrets_root, CLIENT_SECRET)
    if not (v.get("url") and v.get("client_id") and secret):
        return None
    return Account(v["url"], v["client_id"], secret, v.get("collection") or None)
