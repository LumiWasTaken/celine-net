"""The settings that start as a copy of a shipped default (settings/seed.py)."""
from __future__ import annotations

from celine.config import CONFIG_DIR
from celine.settings import seed


def test_the_schedules_and_the_context_shares_are_copied_once(tmp_path):
    seed.seed(tmp_path)
    shipped = [*(CONFIG_DIR / "schedules").glob("*.toml"), *(CONFIG_DIR / "schedules").glob("*.md")]
    assert sorted(p.name for p in seed.schedules_dir(tmp_path).iterdir()) == \
        sorted(p.name for p in shipped) and shipped
    seed.context_path(tmp_path).write_text("notes = 0.2\n")
    seed.seed(tmp_path)
    assert seed.context_path(tmp_path).read_text() == "notes = 0.2\n"
