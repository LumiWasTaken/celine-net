"""API keys typed into Settings: var/secrets/<provider>.key, mode 600, folder 700.

Write-only from the outside: set, replace, delete. Nothing returns a key, not even a
part of it; the API only ever says "set" or "not set" and where the key comes from.
Every key read here is added to errors.set_secrets() through Config.secrets(), so it is
masked in logs and error text.
"""
from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .files import Invalid

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MAX_KEY = 4096


def secrets_dir(data_dir) -> Path:
    return Path(data_dir) / "secrets"


def _path(root: Path, provider: str) -> Path:
    if not _ID.match(provider):
        raise Invalid("provider", "That is not a provider id.")
    return root / f"{provider}.key"


def _ensure_dir(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if (root.stat().st_mode & 0o777) != 0o700:
        os.chmod(root, 0o700)


def set_key(root: Path, provider: str, key: str) -> dict:
    key = (key or "").strip()
    if not key:
        raise Invalid("key", "The key is empty.")
    if len(key) > MAX_KEY or any(c in key for c in "\r\n\0"):
        raise Invalid("key", "That does not look like an API key.")
    p = _path(root, provider)
    _ensure_dir(root)
    fd, tmp = tempfile.mkstemp(prefix=f".{provider}.", suffix=".tmp", dir=root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return state(root, provider)


def delete_key(root: Path, provider: str) -> dict:
    p = _path(root, provider)
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    return state(root, provider)


def read_key(root: Path, provider: str) -> str | None:
    """For the backends only. Never for an API response."""
    try:
        p = _path(root, provider)
    except Invalid:
        return None
    if not p.is_file():
        return None
    try:
        if p.stat().st_mode & 0o077:
            os.chmod(p, 0o600)          # someone loosened it; tighten, the key is still ours
        return p.read_text().strip() or None
    except OSError:
        return None


def state(root: Path, provider: str) -> dict:
    """{"state": "set"|"not set", "set_at": iso|None}: all a caller ever learns."""
    p = _path(root, provider)
    if not p.is_file():
        return {"state": "not set", "set_at": None}
    at = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
    return {"state": "set", "set_at": at.isoformat(timespec="seconds").replace("+00:00", "Z")}


def all_keys(root: Path) -> list[str]:
    """Every stored key value, for redaction."""
    if not root.is_dir():
        return []
    out = []
    for p in root.glob("*.key"):
        try:
            v = p.read_text().strip()
        except OSError:
            continue
        if v:
            out.append(v)
    return out
