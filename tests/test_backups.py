"""The nightly backups (backups.py): made once a night, kept backup_keep_days nights, by
the lifespan's timer whatever is switched on."""
from __future__ import annotations

import lzma
import sqlite3
import tarfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from celine import backups
from celine.backups import nightly


def test_the_nightly_backups_are_made_once_a_night_and_seven_kept(tmp_path):
    nb = tmp_path / "notebook"
    (nb / ".versions").mkdir(parents=True)
    (nb / "a.md").write_text("a")
    (nb / ".versions" / "a.md.1").write_text("old a")
    db = tmp_path / "celine.db"
    sqlite3.connect(db).execute("CREATE TABLE t(x)").connection.commit()
    sqlite3.connect(tmp_path / "log.db").execute("CREATE TABLE e(x)").connection.commit()
    made = nightly(tmp_path, db, nb, date(2026, 10, 16))
    assert [p.relative_to(tmp_path).as_posix() for p in made] == [
        "backups/notebook/2026-10-16.tar.xz", "backups/db/2026-10-16.db.xz",
        "backups/log/2026-10-16.db.xz"]
    with tarfile.open(made[0]) as tar:
        assert {"notebook/a.md", "notebook/.versions/a.md.1"} <= set(tar.getnames())
    restored = tmp_path / "restored.db"
    restored.write_bytes(lzma.decompress(made[1].read_bytes()))
    assert sqlite3.connect(restored).execute("SELECT name FROM sqlite_master").fetchone() \
        == ("t",)
    restored.write_bytes(lzma.decompress(made[2].read_bytes()))
    assert sqlite3.connect(restored).execute("SELECT name FROM sqlite_master").fetchone() \
        == ("e",)
    assert nightly(tmp_path, db, nb, date(2026, 10, 16)) == [], "once a night"
    assert not list(Path(tmp_path, "backups").rglob("*.part"))
    (tmp_path / "backups" / "db" / "notes.xz").write_text("not a night")
    for n in range(1, 10):
        nightly(tmp_path, db, None, date(2026, 10, 16) + timedelta(days=n))
    kept = sorted(p.name for p in (tmp_path / "backups" / "db").iterdir())
    assert len(kept) == 7 + 1 and kept[0] == "2026-10-19.db.xz", "seven nights kept"
    assert "notes.xz" in kept, "a name that is not a night stays"


class Done(Exception):
    pass


@pytest.mark.anyio
async def test_the_timer_keeps_each_nights_backups_at_one_in_the_apps_zone(tmp_path):
    (tmp_path / "notebook").mkdir()
    (tmp_path / "notebook" / "a.md").write_text("a")
    sqlite3.connect(tmp_path / "celine.db").execute("CREATE TABLE t(x)").connection.commit()
    cfg = SimpleNamespace(data_dir=tmp_path, db_path=tmp_path / "celine.db",
                          timezone="Europe/Berlin", her=SimpleNamespace(notebook=tmp_path / "notebook"),
                          r=lambda key: {"backup_keep_days": 7}[key])
    at = [datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc)]      # 22:00 in Berlin
    waits = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        at[0] += timedelta(seconds=seconds)
        if len(waits) == 3:
            raise Done

    with pytest.raises(Done):
        await backups.keep(cfg, clock=lambda: at[0], sleep=sleep)
    assert waits[:2] == [3 * 3600, 24 * 3600]
    assert sorted(p.relative_to(tmp_path).as_posix() for p in (tmp_path / "backups").rglob("*.xz")) == [
        "backups/db/2026-10-17.db.xz", "backups/db/2026-10-18.db.xz",
        "backups/notebook/2026-10-17.tar.xz", "backups/notebook/2026-10-18.tar.xz"]
