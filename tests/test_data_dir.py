"""The data dir on a host install is its user's alone (__main__.own_data_dir), the files
other containers mount from it keep the modes keys.py gives them, and a fresh one after
preflight and the lifespan holds the core's files and nothing of a feature that is off."""
from __future__ import annotations

import argparse
import logging
import os
import socket
import stat
from pathlib import Path

import pytest

from celine import __main__ as main, keys
from celine.__main__ import own_data_dir
from celine.settings import features, files as settings_files


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


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """An empty data dir the app starts on; the root logger put back after."""
    monkeypatch.setenv("CELINE_DATA_DIR", str(tmp_path / "fresh"))
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield tmp_path / "fresh"
    for h in list(root.handlers):
        root.removeHandler(h)
        if h not in handlers:
            h.close()
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _start():
    """preflight, then the lifespan up and down. -> the app's services."""
    cfg = main.preflight(argparse.Namespace(port=_free_port(), dev=False, verbose=False))
    app = main.build_app(cfg, dev=False)
    async with app.router.lifespan_context(app):
        pass
    return app.state.services


def _tree(data: Path) -> set[str]:
    """Every file under `data` and every empty folder (with a trailing slash); keys/ as
    one name, the WAL side files left out."""
    out = set()
    for p in data.rglob("*"):
        rel = p.relative_to(data).as_posix()
        if rel.startswith("keys/"):
            out.add("keys/*")
        elif p.is_file() and not p.name.endswith(("-wal", "-shm")):
            out.add(rel)
        elif p.is_dir() and not any(p.iterdir()):
            out.add(rel + "/")
    return out


def _off(data: Path) -> None:
    settings_files.write(data / "settings", "features", {f.id: False for f in features.CATALOG})


@pytest.mark.anyio
async def test_a_feature_off_makes_none_of_its_files(fresh):
    _off(fresh)
    services = await _start()
    tree = _tree(fresh)
    assert not {p for p in tree if p.startswith((
        "kb/", "documents/", "notebook/appearance/", "settings/schedules/"))}
    assert (services.batches, services.triggers, services.schedules) == (None, None, None)


@pytest.mark.anyio
async def test_the_unsaved_defaults_make_the_files_of_the_features_on(fresh):
    await _start()
    tree = _tree(fresh)
    assert {"kb/documents.db", "documents/", "settings/schedules/radar.toml"} <= tree
