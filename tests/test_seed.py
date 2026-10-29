"""The settings that start as a copy of a shipped default (settings/seed.py): the context
shares at preflight, the schedules at the Scheduler's start."""
from __future__ import annotations

import shutil
from types import SimpleNamespace

import anyio
import pytest

from celine import boot
from celine.config import CONFIG_DIR
from celine.schedules.runs import Scheduler
from celine.settings import seed


def test_the_context_shares_are_copied_once_and_the_schedules_not(tmp_path):
    seed.seed(tmp_path)
    assert not seed.schedules_dir(tmp_path).exists()
    seed.context_path(tmp_path).write_text("notes = 0.2\n")
    seed.seed(tmp_path)
    assert seed.context_path(tmp_path).read_text() == "notes = 0.2\n"


@pytest.mark.anyio
async def test_the_schedulers_start_copies_the_schedules_once_and_reads_them(tmp_path, store):
    folder = seed.schedules_dir(tmp_path / "var" / "settings")
    shutil.rmtree(folder)
    cfg = boot.load()
    assert cfg.schedules == {}
    async with anyio.create_task_group() as tg:
        await Scheduler(SimpleNamespace(cfg=cfg, store=store, dispatcher=None)).run(tg)
        tg.cancel_scope.cancel()
    shipped = [*(CONFIG_DIR / "schedules").glob("*.toml"), *(CONFIG_DIR / "schedules").glob("*.md")]
    assert sorted(p.name for p in folder.iterdir()) == sorted(p.name for p in shipped) and shipped
    assert set(cfg.schedules) == {p.stem for p in shipped if p.suffix == ".toml"}
    (folder / "radar.toml").unlink()
    assert not seed.schedules(tmp_path / "var" / "settings"), "a folder that exists stays as it is"
