"""The data dir on a host install is its user's alone (__main__.own_data_dir), and the
files other containers mount from it keep the modes keys.py gives them."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from celine import keys
from celine.__main__ import own_data_dir


def mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


@pytest.fixture
def open_umask():
    old = os.umask(0o022)
    yield
    os.umask(old)


def test_the_data_dir_and_what_the_app_writes_are_its_users_alone(tmp_path, open_umask):
    data = tmp_path / "srv" / "data"
    own_data_dir(data)
    assert mode(data) == 0o700
    (data / "celine.db").write_text("")
    (data / "logs").mkdir()
    assert (mode(data / "celine.db"), mode(data / "logs")) == (0o600, 0o700)

    was_open = tmp_path / "opened"
    was_open.mkdir()
    was_open.chmod(0o755)
    own_data_dir(was_open)
    assert mode(was_open) == 0o700, "an existing one of its own is closed"


def test_the_files_other_containers_mount_keep_their_modes(tmp_path, open_umask):
    data = tmp_path / "data"
    own_data_dir(data)
    keys.ensure(data)
    got = {p.name: mode(p) for p in (data / "keys").iterdir()}
    assert {n: got[n] for n in ("shell.hash", "browser.hash", "searxng-settings.yml",
                                "connector-clients", "shellbox-llm.key")} == {
        "shell.hash": 0o644, "browser.hash": 0o644, "searxng-settings.yml": 0o644,
        "connector-clients": 0o600, "shellbox-llm.key": 0o600}
