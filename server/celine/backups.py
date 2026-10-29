"""The nightly backups, made every night at AT in the app's zone by the lifespan's timer
(`keep`), whatever is switched on, and kept [runtime] backup_keep_days nights (Settings,
Advanced): older ones are deleted then: its whole notebook as one unit, `.versions`,
`.trash` and its index `.index.db` included, as var/backups/notebook/<night>.tar.xz, the
database as var/backups/db/<night>.db.xz and the logs as var/backups/log/<night>.db.xz
(SQLite's online backup, then xz). A night that has them is not backed up again. Restore
is by hand: unpack the notebook over its own, or unxz a database in place of
var/celine.db or var/log.db with the app stopped."""
from __future__ import annotations

import logging
import lzma
import shutil
import sqlite3
import tarfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import anyio

from .clock import zone
from .db.migrate import log_path

log = logging.getLogger("celine.backups")


FOLDERS = ("notebook", "db", "log")
AT = time(1, 0)            # after the evening, before the first shipped routine


def next_at(now: datetime, tz) -> datetime:
    """The next AT in `tz` after `now`; its date names the night."""
    local = now.astimezone(tz)
    at = local.replace(hour=AT.hour, minute=AT.minute, second=0, microsecond=0)
    return at if at > local else at + timedelta(days=1)


async def keep(cfg, clock=lambda: datetime.now(timezone.utc), sleep=anyio.sleep) -> None:
    """The lifespan's timer: the night's backups at every AT."""
    while True:
        at = next_at(clock(), zone(cfg))
        await sleep((at - clock()).total_seconds())
        try:
            await anyio.to_thread.run_sync(nightly, cfg.data_dir, cfg.db_path, cfg.her.notebook,
                                           at.date(), int(cfg.r("backup_keep_days")))
        except Exception:
            log.exception("backup failed", extra={"event": "backup.failed"})


def paths(data_dir: Path, night: date) -> tuple[Path, Path, Path]:
    root = data_dir / "backups"
    return (root / "notebook" / f"{night.isoformat()}.tar.xz",
            root / "db" / f"{night.isoformat()}.db.xz",
            root / "log" / f"{night.isoformat()}.db.xz")


def nightly(data_dir: Path, db_path: Path, notebook: Path | None, night: date,
            keep_days: int = 7) -> list[Path]:
    """The night's backups that are not there yet. -> the files written."""
    nb, db, lg = paths(data_dir, night)
    made = []
    if notebook is not None and notebook.is_dir() and not nb.exists():
        nb.parent.mkdir(parents=True, exist_ok=True)
        part = nb.with_suffix(".part")
        with tarfile.open(part, "w:xz") as tar:
            tar.add(notebook, arcname="notebook")
        part.rename(nb)
        made.append(nb)
    for src, out in ((db_path, db), (log_path(db_path), lg)):
        if src.exists() and not out.exists():
            _sqlite(src, out)
            made.append(out)
    for p in made:
        log.info("backup kept", extra={"event": "backup.kept", "path": str(p)})
    prune(data_dir, night, keep_days)
    return made


def _sqlite(src: Path, out: Path) -> None:
    """SQLite's online backup of `src`, xz-packed, at `out`."""
    out.parent.mkdir(parents=True, exist_ok=True)
    copy = out.with_suffix(".part")
    a, b = sqlite3.connect(src), sqlite3.connect(copy)
    try:
        a.backup(b)
    finally:
        b.close()
        a.close()
    packed = out.with_suffix(".xz.part")
    with copy.open("rb") as f, lzma.open(packed, "wb") as z:
        shutil.copyfileobj(f, z)
    copy.unlink()
    packed.rename(out)


def prune(data_dir: Path, night: date, keep_days: int) -> list[Path]:
    """The backups of nights keep_days or more before `night` deleted. -> the files
    deleted. A name that is not a night is left alone."""
    gone = []
    for folder in FOLDERS:
        for p in (data_dir / "backups" / folder).glob("*.xz"):
            try:
                d = date.fromisoformat(p.name.split(".", 1)[0])
            except ValueError:
                continue
            if (night - d).days >= keep_days:
                p.unlink(missing_ok=True)
                gone.append(p)
                log.info("backup deleted", extra={"event": "backup.deleted",
                                                  "path": str(p)})
    return gone
