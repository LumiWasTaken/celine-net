"""SQLite plumbing shared by every knowledge base file.

Each knowledge base is one file. Reads open a connection per thread and keep it;
writes open their own short-lived connection and rely on WAL plus busy_timeout, so an
import committing a batch never blocks the chat list. No connection is shared across
threads.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from itertools import batched
from pathlib import Path
from typing import Any, TypeVar

import anyio
import sqlite_vec

from ..db.migrate import migrate

CHAT_MIGRATIONS = Path(__file__).parent / "migrations" / "chat"
DOCUMENTS_MIGRATIONS = Path(__file__).parent / "migrations" / "documents"

T = TypeVar("T")

_local = threading.local()


IN_MAX = 500          # parameters in one SQL `IN (...)`


def in_batches(xs) -> Iterator[tuple]:
    """`xs` in tuples of at most IN_MAX, for `IN (...)` queries."""
    return batched(xs, IN_MAX)


def connect(path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)                   # vector distances (kb/passages.py)
    conn.enable_load_extension(False)
    return conn


def kv(conn: sqlite3.Connection, key: str) -> str | None:
    """A value of the file's `kv` table, None when it has none."""
    r = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return r[0] if r else None


def set_kv(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))


class KBFile:
    """One SQLite file. `read` and `write` take a sync callable and run it in a
    worker thread, the same shape as the core Store. `migrations` is the folder of
    numbered SQL files for this kind of knowledge base."""

    def __init__(self, path: Path, migrations: Path):
        self.path = path
        migrate(path, migrations)

    def _reader(self) -> sqlite3.Connection:
        """This thread's connection to the file. Reopened when the file on disk is a
        different one than when it was opened (a restored backup, a copied-in
        knowledge base), so a replaced file is never served from the old inode."""
        cache: dict[str, tuple[sqlite3.Connection, int]] = getattr(_local, "conns", None) or {}
        _local.conns = cache
        key = str(self.path)
        try:
            ino = os.stat(self.path).st_ino
        except FileNotFoundError:
            ino = -1
        hit = cache.get(key)
        if hit is not None and hit[1] == ino:
            return hit[0]
        if hit is not None:
            try:
                hit[0].close()
            except sqlite3.Error:
                pass
        conn = connect(self.path)
        cache[key] = (conn, os.stat(self.path).st_ino)
        return conn

    @contextmanager
    def writer(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.path)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def read_sync(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        conn = self._reader()
        try:
            return fn(conn)
        finally:
            if conn.in_transaction:
                conn.rollback()

    def write_sync(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        with self.writer() as conn:
            return fn(conn)

    async def read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return await anyio.to_thread.run_sync(self.read_sync, fn)

    async def write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return await anyio.to_thread.run_sync(self.write_sync, fn)

    def get_json(self, key: str, default: Any = None) -> Any:
        """A JSON value of the `kv` table (blocking), `default` when it has none."""
        v = self.read_sync(lambda c: kv(c, key))
        return default if v is None else json.loads(v)

    def set_json(self, key: str, value: Any) -> None:
        """Store a JSON value in the `kv` table (blocking); None removes the key."""
        if value is None:
            self.write_sync(lambda c: c.execute("DELETE FROM kv WHERE key = ?", (key,)))
        else:
            self.write_sync(lambda c: set_kv(c, key, json.dumps(value)))
