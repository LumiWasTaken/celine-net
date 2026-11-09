"""Bastet's own model role (D17): `guard` holds its decision model; a saved roles file
gets small's list once, by migration."""
from __future__ import annotations

import importlib.util
import tomllib

from celine.db.migrate import MIGRATIONS_DIR
from celine.guard import small_role
from celine.settings.registry import ROLES


def test_bastet_asks_its_own_guard_role():
    assert small_role.ROLE == "guard" and ROLES["guard"]["caps"] == ["chat"]


def test_the_migration_gives_the_guard_role_the_small_list_once(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "m0023", next(MIGRATIONS_DIR.glob("0023_*.py")))
    step = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(step)
    db = tmp_path / "celine.db"
    step.up(None, db)                                 # no roles file: nothing to do
    path = tmp_path / "settings" / "roles.toml"
    path.parent.mkdir(parents=True)
    path.write_text('[chat]\nchain = ["a/big"]\n\n[small]\nchain = ["a/fast", "b/tiny"]\n')
    step.up(None, db)
    got = tomllib.loads(path.read_text())
    assert got["guard"] == {"chain": ["a/fast", "b/tiny"]} and got["small"]["chain"] == [
        "a/fast", "b/tiny"]
    before = path.read_bytes()
    step.up(None, db)
    assert path.read_bytes() == before, "run again, it writes nothing"
    path.write_text('[small]\nchain = ["a/fast"]\n\n[guard]\nchain = ["c/judge"]\n')
    step.up(None, db)
    assert tomllib.loads(path.read_text())["guard"]["chain"] == ["c/judge"], \
        "a guard list the user saved stays"
