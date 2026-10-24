"""Bastet's prompts as files: config/guard/*.md and questions.toml. Her persona, talk
and note are only the defaults of the texts the owner edits on Settings, Guard
(persona.py); the judges' prompts are read from here alone (a Docker install mounts its
own config/guard, docs/customize.md). Read at call time, cached by the file's mtime: an
edit applies at the next ask.

A prompt file may start with one `<!-- ... -->` line saying what it reads and how it is
answered; it is never sent. Slots are `{name}` (filled by code, guard/judge.py,
questions.py, banners.py, talk.py) and `{{name}}`, `{{user}}` (its name, the owner's).
Persona text never goes into a judge's prompt (judges are neutral and calibrated); it
lives only in persona.py's texts.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from ..config import CONFIG_DIR

DIR = CONFIG_DIR / "guard"
_HEADER = re.compile(r"\A<!--.*?-->\n", re.S)
_cache: dict[Path, tuple[int, str]] = {}


def _read(p: Path) -> str:
    t = p.stat().st_mtime_ns
    got = _cache.get(p)
    if got is None or got[0] != t:
        got = (t, p.read_text())
        _cache[p] = got
    return got[1]


def text(name: str) -> str:
    """The prompt `name` (config/guard/<name>.md), its header line left out."""
    return _HEADER.sub("", _read(DIR / f"{name}.md"), count=1).strip("\n")


def questions() -> dict[str, dict[str, str]]:
    """questions.toml: {"ask": {rule: question}, "after": {...}, "answer": {...}}."""
    return tomllib.loads(_read(DIR / "questions.toml"))
