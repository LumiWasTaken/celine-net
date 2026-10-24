"""Numbered SQL migrations, applied in order, each in one transaction. The first file is
the whole schema (a fresh database applies it); later files apply on top of it.

Every SQLite file (the main db, log.db beside it, the notebook's index, each knowledge
base, auth.db) uses this runner with its own directory, and keeps its version in the
file's header (PRAGMA user_version), set in the transaction of the migration.

Before a migration changes a file that has data, the file is copied beside it as
<file>.pre-<version>.bak (an online backup, so a running reader is no matter); the
last COPIES_KEPT copies of each file are kept."""
from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
from pathlib import Path

log = logging.getLogger("celine.db")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
LOG_MIGRATIONS = MIGRATIONS_DIR / "log"
NOTEBOOK_MIGRATIONS = MIGRATIONS_DIR / "notebook"
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
BUSY_MS = 5000          # how long a connection waits for another's lock (core/store.py too)
COPIES_KEPT = 3         # copies made before a migration, per file


def _migrations(directory: Path | None = None) -> list[tuple[int, Path]]:
    out = []
    for p in sorted((directory or MIGRATIONS_DIR).glob("*.sql")):
        m = _NAME.match(p.name)
        if not m:
            raise RuntimeError(f"migration {p.name} is not NNNN_lower_snake.sql")
        out.append((int(m.group(1)), p))
    versions = [v for v, _ in out]
    if len(set(versions)) != len(versions):
        raise RuntimeError(f"duplicate migration numbers: {versions}")
    return out


def _version(conn: sqlite3.Connection, name: str) -> int:
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v == 0 and conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'").fetchone():
        raise RuntimeError(f"{name} has no schema version; delete it and let Celine "
                           f"build it again.")
    return v


def schema_version(conn: sqlite3.Connection) -> int:
    """celine.db's version, 0 for a fresh one."""
    return _version(conn, "celine.db")


@contextlib.contextmanager
def read_only(path: Path):
    """A read-only connection to a database file, closed after."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        yield conn
    finally:
        conn.close()


def log_path(db_path: Path) -> Path:
    """var/log.db: the append-only tables, beside celine.db."""
    return db_path.parent / "log.db"


def notebook_db(db_path: Path) -> Path:
    """var/notebook/.index.db: the index of its notes, beside them (hidden, as every
    notebook walk skips dot entries)."""
    return db_path.parent / "notebook" / ".index.db"


def migrate_all(db_path: Path) -> int:
    """celine.db, log.db beside it and the notebook's index, each its own chain. ->
    celine.db's version."""
    migrate(log_path(db_path), LOG_MIGRATIONS)
    migrate(notebook_db(db_path), NOTEBOOK_MIGRATIONS)
    return migrate(db_path)


def migrate(db_path: Path, migrations_dir: Path | None = None) -> int:
    """Bring the database to the latest schema of `migrations_dir`. Returns the version."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    migrations = _migrations(migrations_dir)
    latest = migrations[-1][0] if migrations else 0

    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        # off while tables are rebuilt, or DROP TABLE fires ON DELETE actions; checked after
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(f"PRAGMA busy_timeout={BUSY_MS}")
        current = _version(conn, db_path.name)
        if current > latest:
            raise RuntimeError(
                f"{db_path.name} is at schema version {current} but this build only "
                f"knows {latest}."
            )
        baseline = migrations[0][0] if migrations else 0
        if 0 < current < baseline:
            raise RuntimeError(
                f"{db_path.name} is at schema version {current}, older than "
                f"{migrations[0][1].name} this build starts from; delete it and let Celine "
                f"build it again."
            )
        pending = [(v, p) for v, p in migrations if v > current]
        if pending and current > 0:
            _copy(conn, db_path, latest)
        for version, path in pending:
            _apply(conn, path, version)
        return latest
    finally:
        conn.close()


def _copy(conn: sqlite3.Connection, db_path: Path, version: int) -> None:
    """The file as it is, to <file>.pre-<version>.bak beside it (through a temp name, so a
    copy is whole or absent); the older copies past COPIES_KEPT go."""
    out = db_path.with_name(f"{db_path.name}.pre-{version}.bak")
    tmp = out.with_name(out.name + ".tmp")
    tmp.unlink(missing_ok=True)
    dst = sqlite3.connect(tmp)
    try:
        conn.backup(dst)
    finally:
        dst.close()
    tmp.replace(out)
    log.info("copied before migrating", extra={"event": "db.copy", "file": str(out)})
    pattern = re.compile(re.escape(db_path.name) + r"\.pre-(\d+)\.bak$")
    copies = sorted((int(mm.group(1)), q) for q in db_path.parent.iterdir()
                    if (mm := pattern.match(q.name)))
    for _, old in copies[:-COPIES_KEPT]:
        old.unlink()


def _apply(conn: sqlite3.Connection, path: Path, version: int) -> None:
    """One script and its version bump in one transaction."""
    # LogRecord reserves name/module/filename as extra keys
    log.info("applying migration", extra={"event": "db.migrate", "version": version,
                                          "migration": path.name})
    # executescript() issues an implicit COMMIT first, so BEGIN goes inside the script;
    # the COMMIT comes after the foreign key check, so a migration that breaks one is
    # rolled back with its version bump
    script = f"BEGIN;\n{path.read_text()}\nPRAGMA user_version = {int(version)};\n"
    try:
        conn.executescript(script)
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(f"migration {path.name} left {len(broken)} broken "
                               f"foreign key rows: {broken[:3]}")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
