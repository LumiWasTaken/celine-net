"""var/settings/*.toml: read, validate, write atomically, version.

`config/` stays read-only hand-written configuration. What the Settings screens change
is written here: plain TOML, readable and editable by hand, reloaded on change (the
config watcher watches this folder too). A file that does not exist yet is an empty
section; the first save writes it.

Every section has a version: the first 16 hex digits of the SHA-256 of its canonical
JSON. A PUT with `If-Match: <version>` that no longer matches is refused (412), so two
tabs never overwrite each other silently. A PUT without it simply replaces the section.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import tomllib
from pathlib import Path
from typing import Any

from ..errors import CelineError
from . import tomlw

HEADER = ("Written by celine-net Settings. Hand edits are fine. The app reads them when "
          "you save.\nThe app does not keep comments you add here when it writes this "
          "file.")

SECTIONS = ("providers", "roles", "tools", "general", "email", "vault", "triggers",
            "appearance", "guard", "features", "services", "address")

_lock = threading.Lock()


class Invalid(CelineError):
    """A value the settings refuse: 400 with the field path and one plain sentence."""

    def __init__(self, field: str, message: str):
        super().__init__("bad_request", message, 400, {"field": field})
        self.field = field


class Conflict(CelineError):
    def __init__(self, section: str, current: str):
        super().__init__("version_conflict",
                         f"The {section} settings changed since you loaded them. "
                         f"Reload and try again.", 412, {"version": current})


def version(data: Any) -> str:
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                     default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def settings_dir(cfg) -> Path:
    return Path(cfg.data_dir) / "settings"


def path_for(root: Path, section: str) -> Path:
    """A core section's file, or a plugin's with a form (its id)."""
    from .. import plugins
    if section not in SECTIONS and section not in {p.id for p in plugins.with_form()}:
        raise ValueError(section)
    return root / f"{section}.toml"


def read(root: Path, section: str) -> tuple[dict[str, Any] | None, str | None]:
    """(data, error). data None = the file does not exist. A broken file gives
    (None, message): at startup the section is empty, on a reload the last good
    values stay."""
    p = path_for(root, section)
    if not p.is_file():
        return None, None
    try:
        with p.open("rb") as f:
            return tomllib.load(f), None
    except tomllib.TOMLDecodeError as e:
        return None, str(e)
    except OSError as e:
        return None, f"{p.name} could not be read: {e.strerror}"


def write(root: Path, section: str, data: dict[str, Any]) -> None:
    write_toml(path_for(root, section), data)


def write_toml(path: Path, data: dict[str, Any]) -> None:
    """A settings file as TOML (a section, a schedule), atomically."""
    text = tomlw.dumps(_strip_none(data), HEADER)
    tomllib.loads(text)                       # never write a file we cannot read back
    write_bytes(path, text.encode())


def write_bytes(path: Path, data: bytes) -> None:
    """Atomic: temp file in the same folder, fsync, rename. Serialised by one lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp",
                                   dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def check_version(section: str, current: dict[str, Any], if_match: str | None) -> None:
    if not if_match:
        return
    want = if_match.strip().removeprefix("W/").strip('"')
    have = version(current)
    if want != have:
        raise Conflict(section, have)


def _strip_none(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _strip_none(x) for k, x in v.items() if x is not None}
    if isinstance(v, list):
        return [_strip_none(x) for x in v]
    return v
