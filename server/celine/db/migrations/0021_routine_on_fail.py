"""A Routine says what happens when a run fails (`on_fail`, schedules/__init__.py): the
shipped Daydream, Followed sites and Watch videos are "skip". Their files in
settings/schedules (beside celine.db) that lack the key get it; every other file reads
"retry" without it. A file that has the key, or does not parse, stays as it is; run
again, it writes nothing."""
from __future__ import annotations

import tomllib
from pathlib import Path

SKIP = ("daydream", "radar", "watch")


def up(conn, db_path) -> None:
    from celine.settings.files import write_toml
    folder = Path(db_path).parent / "settings" / "schedules"
    for name in SKIP:
        path = folder / f"{name}.toml"
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text())
        except (tomllib.TOMLDecodeError, OSError):
            continue
        if "on_fail" not in data:
            write_toml(path, {**data, "on_fail": "skip"})
