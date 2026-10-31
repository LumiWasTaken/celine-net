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

from . import form
from .files import Invalid

CLIENT_SECRET = "vault-api"     # var/secrets/vault-api.key

FIELDS: list[dict[str, Any]] = [
    {"key": "url", "label": "Server", "type": "text",
     "help": "The Vaultwarden address, such as https://vault.example.com.", "default": "",
     "word": True},
    {"key": "client_id", "label": "Client ID", "type": "text",
     "help": "From the web vault: Settings, Security, Keys, View API key.", "default": "",
     "word": True},
    {"key": "collection", "label": "Collection", "type": "text",
     "help": "The name of the organization collection you share with the agent (its ID if "
             "two have the same name). The agent sees and saves logins only in this "
             "collection.",
     "default": "", "word": True},
]
_BY_KEY = {f["key"]: f for f in FIELDS}


def value(vault: dict[str, Any], key: str) -> Any:
    return vault.get(key, _BY_KEY[key]["default"])


def validate(data: Any) -> dict[str, Any]:
    out = form.validate("vault", FIELDS, data)
    if "url" in out and (urlsplit(out["url"]).scheme != "https"
                         or not urlsplit(out["url"]).netloc):
        # bw takes no other: "Insecure URL not allowed"
        raise Invalid("vault.url", "This must be an https address, such as "
                                   "https://vault.example.com.")
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
