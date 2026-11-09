"""A Routine's `partner` is `reviewer` and its `rounds` is `turns` (schedules/__init__.py).
In every schedule file in settings/schedules (beside celine.db) the two keys get their
new names, their values and the rest of the file kept as they were; a reviewer prompt
`<x>-partner.md` (the name a save gave it) is `<x>-reviewer.md`, the key's value with
it. A file the line rewrite does not read back right (a key in quotes, a line of the
prompt that starts with one) is written again whole (write_toml). A file that does not parse stays as it is; run again, it writes nothing."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

NAMES = {"partner": "reviewer", "rounds": "turns"}
KEY = re.compile(r"^(partner|rounds)(?=[ \t]*=)", re.M)


def _moved(folder: Path, file: str) -> str:
    """The reviewer prompt's name once `<x>-partner.md` moved to `<x>-reviewer.md`; it
    stays where a file of the new name is there already."""
    if not file.endswith("-partner.md"):
        return file
    new = file.removesuffix("-partner.md") + "-reviewer.md"
    old_path, new_path = folder / file, folder / new
    if old_path.is_file() and not new_path.exists():
        old_path.rename(new_path)
    return file if old_path.exists() else new


def up(conn, db_path) -> None:
    from celine.settings.files import write_bytes, write_toml
    folder = Path(db_path).parent / "settings" / "schedules"
    for path in sorted(folder.glob("*.toml")):
        try:
            text = path.read_text()
            data = tomllib.loads(text)
        except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
            continue
        want = {NAMES.get(k, k): v for k, v in data.items()}
        file = want.get("reviewer")
        if isinstance(file, str) and file:
            want["reviewer"] = _moved(folder, file)
        if want == data:
            continue
        new = KEY.sub(lambda m: NAMES[m[1]], text)
        if want.get("reviewer") != file:
            new = re.sub(rf"^(reviewer[ \t]*=[ \t]*[\"']){re.escape(file)}(?=[\"'])",
                         lambda m: m[1] + want["reviewer"], new, count=1, flags=re.M)
        try:
            kept = tomllib.loads(new) == want
        except tomllib.TOMLDecodeError:
            kept = False
        if kept:
            write_bytes(path, new.encode())
        else:
            write_toml(path, want)
