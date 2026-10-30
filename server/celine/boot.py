"""The whole configuration in one Config: config/celine.toml (config.load), the files
each owner reads (mcp.toml, kb.toml, skills.toml, llm.toml, the presets), var/settings and
its row. It reads and never writes: startup (__main__.preflight) makes the data dir, the
keys and the seeded settings once, before the first load; reload.py loads again."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config, her, keys, plugins, schedules, settings
from .backends.llm import QUEUE_KEYS
from .comfyui import presets
from .config import Config, Problem, shown_path, read_toml, tables
from .db.migrate import read_only
from .kb import read_config as read_kb
from .mcp import read_servers
from .settings import features, seed
from .skills import BUILTIN
from .turns import loads


def load() -> Config:
    cfg = config.load()
    p = cfg.problems
    # every manifest before the settings, so the plugins' switches and forms are known
    reserved = frozenset(f.id for f in features.BASE) | frozenset(settings.files.SECTIONS)
    p += [Problem("plugins", m) for m in plugins.scan(cfg.data_dir, reserved,
                                                                 settings.form.check_toml)]
    p += [Problem("plugins", f"plugins/{x.id}: there is no group {x.manifest['group']!r}, "
                             f"it gets a card of its own")
          for x in plugins.all() if x.manifest.get("group", "") not in ("", *features.GROUPS)]

    def file(name: str) -> tuple[str, dict]:
        path = config.CONFIG_DIR / name
        return shown_path(path), read_toml(path, p)

    cfg.mcp = read_servers(*file("mcp.toml"), p, keys.env(cfg.data_dir))
    kb = read_kb(*file("kb.toml"), p)
    cfg.embeddings = kb.pop("embeddings", {})
    cfg.media = kb.pop("media", {})
    cfg.kb = kb
    cfg.skills = tables(*file("skills.toml"),
                        {name: {"enabled": "bool"} for name, _, _ in BUILTIN}, p)
    cfg.llm = tables(*file("llm.toml"), {"queue": QUEUE_KEYS}, p)
    for kind in presets.FOLDERS:
        cfg.presets[kind], found = presets.load(kind)
        p += found
    settings.apply(cfg)      # var/settings: providers, model roles, tools, general
    # its row, read only: the database is migrated at startup and the row written
    # through the Store (her.seed, her.refresh); none yet before the first start
    her.apply(cfg, _rows(cfg.db_path, her.read_row), p)
    root = settings.files.settings_dir(cfg)
    cfg.schedules = schedules.load(seed.schedules_dir(root), p, cfg.substitute)
    cfg.context = loads.read_shares(seed.context_path(root), p)
    return cfg


def _rows(db_path: Path, read):
    """`read(connection)` on the database, read only; None when it has no such table yet."""
    if not db_path.is_file():
        return None
    with read_only(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            return read(conn)
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e):
                raise
            return None
