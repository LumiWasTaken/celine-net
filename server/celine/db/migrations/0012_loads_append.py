"""Loads append (skills/meta.py): no skill or tool is carried by a row any more, so the
`context_loaded` rows of kind skill and tool go, and so does the `tools` budget in
settings/context.toml (beside celine.db). Run again, it writes the same."""
from __future__ import annotations

import re
from pathlib import Path


def up(conn, db_path) -> None:
    conn.execute("BEGIN")
    conn.execute("DELETE FROM context_loaded WHERE kind IN ('skill', 'tool')")
    conn.execute("COMMIT")
    drop_keys(Path(db_path).parent / "settings" / "context.toml", ("tools",))


def drop_keys(path: Path, keys: tuple[str, ...]) -> None:
    """The lines that set `keys` leave the TOML file; the rest stays as written."""
    if not path.is_file():
        return
    rx = re.compile(r"\s*(" + "|".join(map(re.escape, keys)) + r")\s*=")
    text = path.read_text()
    kept = [ln for ln in text.splitlines(keepends=True) if not rx.match(ln)]
    if len(kept) != len(text.splitlines(keepends=True)):
        path.write_text("".join(kept))
