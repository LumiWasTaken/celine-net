"""The settings that start as a copy of a shipped default: the context shares and the
schedules. config/ ships the defaults; each is copied into var/settings/ when its place
there does not exist (a deleted folder comes back), and from then on only var/settings is
read: a schedule shipped later reaches an install that has the folder only by hand or on
the Routines page. The agent's own row is celine.her.

- `var/settings/context.toml`: config/context.toml, the shares of the window it
  carries notes in, and the tokens of tools (turns/loads.py). At preflight (`seed`).
- `var/settings/schedules/`: config/schedules/*.toml and the partner prompts *.md
  (celine/schedules). At the Scheduler's start (`schedules`), so only with routines on.
Bastet's persona and voice are never seeded: config/guard/ is their default (guard/persona.py).
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ..config import CONFIG_DIR
from .files import write_bytes


def schedules_dir(root: Path) -> Path:
    return root / "schedules"


def _copy(src: Path, dst: Path, patterns: tuple[str, ...]) -> None:
    """The folder's files matching `patterns` into `dst`, which is made only once they
    are all copied, so a crash halfway copies again on the next start."""
    tmp = dst.with_name(f".{dst.name}.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for pattern in patterns:
        for p in src.glob(pattern):
            shutil.copyfile(p, tmp / p.name)
    tmp.rename(dst)


def context_path(root: Path) -> Path:
    return root / "context.toml"


def seed(root: Path) -> None:
    """The context shares when they do not exist yet; never touches them when they do."""
    root.mkdir(parents=True, exist_ok=True)
    if not context_path(root).exists():
        write_bytes(context_path(root), (CONFIG_DIR / "context.toml").read_bytes())


def schedules(root: Path) -> bool:
    """The shipped schedules when the folder does not exist yet. True: it copied them."""
    if schedules_dir(root).exists():
        return False
    _copy(CONFIG_DIR / "schedules", schedules_dir(root), ("*.toml", "*.md"))
    return True
