"""Settings, Features saved every service's address at once, so an address compose's variable
had filled (CELINE_SHELL_URL, CELINE_BROWSER_URL, CELINE_SEARXNG_URL) was kept as if typed,
and a later change of that variable never reached the app. Each service is saved on its own
now and the variable fills every service services.toml does not name (settings/services.py):
a saved address equal to its variable is dropped, so the variable fills it again. A file
that does not parse stays as it is; run again, it writes nothing."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

ENV = {"shellbox": "CELINE_SHELL_URL", "browser": "CELINE_BROWSER_URL",
       "searxng": "CELINE_SEARXNG_URL"}


def _copy(sid: str, block) -> bool:
    env = os.environ.get(ENV.get(sid, ""), "").strip().rstrip("/")
    return bool(env) and isinstance(block, dict) and \
        str(block.get("url") or "").rstrip("/") == env


def up(conn, db_path) -> None:
    from celine.settings.files import write_toml
    path = Path(db_path).parent / "settings" / "services.toml"
    if not path.is_file():
        return
    try:
        data = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        return
    keep = {sid: block for sid, block in data.items() if not _copy(sid, block)}
    if keep != data:
        write_toml(path, keep)
