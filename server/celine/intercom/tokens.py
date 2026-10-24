"""Named tokens: a host script sends on the intercom as `system:<name>` with
`Authorization: Bearer <token>`. The user makes them in Settings, Security; the token is
shown once and stored as its sha256. Revoking hides a token, the row stays."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3

from ..core.ids import now

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def bearer(headers) -> str | None:
    """The token of an `Authorization: Bearer` header (ASGI header pairs), else None."""
    for k, v in headers:
        if k == b"authorization":
            kind, _, token = v.decode("latin-1").partition(" ")
            if kind.lower() == "bearer" and token.strip():
                return token.strip()
    return None


def name_of(c: sqlite3.Connection, token: str) -> str | None:
    """The name of the token that is not revoked, else None. Constant-time compare."""
    want = _hash(token)
    found = None
    for r in c.execute("SELECT name, hash FROM intercom_tokens WHERE revoked_at IS NULL"):
        if hmac.compare_digest(bytes(r["hash"]), want):
            found = r["name"]
    return found


def listed(c: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in c.execute(
        "SELECT id, name, created_at FROM intercom_tokens WHERE revoked_at IS NULL "
        "ORDER BY created_at")]


def make(c: sqlite3.Connection, name: str) -> str:
    """A new token named `name`; the token itself is returned only here.
    ValueError: a bad name or one in use."""
    if not NAME.fullmatch(name):
        raise ValueError("A name is letters, digits, dot, dash or underscore, up to 40.")
    if c.execute("SELECT 1 FROM intercom_tokens WHERE name=? AND revoked_at IS NULL",
                 (name,)).fetchone():
        raise ValueError(f"A token named {name} exists.")
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO intercom_tokens (name, hash, created_at) VALUES (?,?,?)",
              (name, _hash(token), now()))
    return token


def revoke(c: sqlite3.Connection, tid: int) -> bool:
    return c.execute("UPDATE intercom_tokens SET revoked_at=? WHERE id=? AND "
                     "revoked_at IS NULL", (now(), tid)).rowcount > 0
