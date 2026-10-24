"""The stack's own keys, made once into <data>/keys and never typed by anyone.

`ensure(data_dir)` writes what is missing: one random key per service (`<name>.key`,
mode 600) and what the other containers get instead of the key itself: `shell.hash`
and `browser.hash` (the sha256 the boxes check), `connector-clients` (the connectors'
list of clients, `live:<key>:owner`) and `searxng-settings.yml` (config/searxng/settings.yml
with `server.secret_key` filled). Compose mounts those four from the volume; the app
reads the keys with `read(data_dir, name)` and shows a box's hash with
`key_hash(data_dir, name)` (Setup, for a box that runs elsewhere). Startup runs ensure
once (__main__.preflight), before the app reports healthy, which is what the other
containers wait for (docker-compose.yml); `celine keys [dir]` runs it on its own."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
from pathlib import Path

from .config import CONFIG_DIR

NAMES = ("shell", "browser", "connector", "shellbox-llm", "searxng")
SEARXNG_SETTINGS = CONFIG_DIR / "searxng" / "settings.yml"


def sha256(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _file(data_dir: Path, name: str) -> str:
    try:
        return (Path(data_dir) / "keys" / name).read_text().strip()
    except FileNotFoundError:
        return ""


def read(data_dir: Path, name: str) -> str:
    """One key, "" when ensure() never ran on this data dir."""
    return _file(data_dir, f"{name}.key")


def values(data_dir: Path) -> list[str]:
    """Every key there is, for the redaction set (errors.set_secrets, source "keys")."""
    return [v for name in NAMES if (v := read(data_dir, name))]


def key_hash(data_dir: Path, name: str) -> str:
    """The sha256 of a key as its box checks it (<name>.hash): what a route shows, so the
    user puts it into a box that runs elsewhere. "" when ensure() never ran."""
    return _file(data_dir, f"{name}.hash")


def _put(path: Path, text: str, mode: int) -> bool:
    """Write `text` when the file differs; True when it was written."""
    if path.exists() and path.read_text() == text:
        return False
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.chmod(mode)
    os.replace(tmp, path)
    return True


def ensure(data_dir: Path) -> list[str]:
    """The keys dir with every key and derived file in it. Idempotent: an existing key
    is kept, the derived files follow the keys. Returns the names of the files written."""
    d = data_dir / "keys"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    written: list[str] = []
    keys: dict[str, str] = {}
    for name in NAMES:
        p = d / f"{name}.key"
        if not p.exists() or not p.read_text().strip():
            _put(p, secrets.token_hex(24) + "\n", 0o600)
            written.append(p.name)
        keys[name] = p.read_text().strip()
    derived = {
        "shell.hash": (sha256(keys["shell"]) + "\n", 0o644),
        "browser.hash": (sha256(keys["browser"]) + "\n", 0o644),
        "connector-clients": (f"live:{keys['connector']}:owner\n", 0o600),
        # searxng runs as its own user, so it must be readable by it
        "searxng-settings.yml": (_searxng_settings(keys["searxng"]), 0o644),
    }
    for name, (text, mode) in derived.items():
        if _put(d / name, text, mode):
            written.append(name)
    return written


def _searxng_settings(secret: str) -> str:
    text = SEARXNG_SETTINGS.read_text()
    line = f"  secret_key: \"{secret}\"\n"
    if re.search(r"^  secret_key:", text, re.M):
        return re.sub(r"^  secret_key:.*\n", line, text, count=1, flags=re.M)
    if not re.search(r"^server:\n", text, re.M):
        raise ValueError(f"{SEARXNG_SETTINGS} has no server: block for the secret_key")
    return re.sub(r"^server:\n", "server:\n" + line, text, count=1, flags=re.M)


def env(data_dir: Path) -> dict[str, str]:
    """What config/mcp.toml's `${...}` may ask for when the environment lacks it."""
    return {"CONNECTOR_KEY": read(data_dir, "connector"),
            "CONNECTOR_CLIENTS_FILE": str(data_dir / "keys" / "connector-clients")}
