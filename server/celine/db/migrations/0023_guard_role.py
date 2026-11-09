"""Bastet has its own model role, `guard` (settings/registry.py ROLES): its decision model,
notes and answers moved off `small` (Titles and descriptions). A saved roles.toml (in
settings, beside celine.db) without a guard role gets small's list as its own; a file that
has one, or does not parse, stays as it is; run again, it writes nothing."""
from __future__ import annotations

import tomllib
from pathlib import Path


def up(conn, db_path) -> None:
    from celine.settings.files import write_toml
    path = Path(db_path).parent / "settings" / "roles.toml"
    if not path.is_file():
        return
    try:
        data = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        return
    chain = (data.get("small") or {}).get("chain") if isinstance(data.get("small"), dict) else None
    if "guard" not in data and chain:
        write_toml(path, {**data, "guard": {"chain": list(chain)}})
