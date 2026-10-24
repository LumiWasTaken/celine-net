from __future__ import annotations

from pathlib import Path

import pytest

from celine.db.migrate import log_path, migrate_all, notebook_db
from celine.core.store import Store


@pytest.fixture
def anyio_backend():
    # asyncio only: uvicorn runs on asyncio and a store that passes under trio
    # but not asyncio would be a false positive.
    return "asyncio"


class Clock:
    """A clock that moves only when the test moves it, from a fixed start.
    `clock()` the seconds, `clock.at` the UTC datetime, `clock.iso()` as tables store it."""

    START = (2026, 3, 11, 10, 0)            # a Wednesday morning, far from midnight

    def __init__(self):
        from datetime import datetime, timezone
        self.at = datetime(*self.START, tzinfo=timezone.utc)

    def __call__(self) -> float:
        return self.at.timestamp()

    def iso(self, seconds: float = 0) -> str:
        """The time `seconds` from now, as the tables store it."""
        from datetime import timedelta

        from celine.core.ids import iso
        return iso(self.at + timedelta(seconds=seconds))

    def advance(self, seconds: float) -> None:
        from datetime import timedelta
        self.at += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture(scope="session")
def _template_db(tmp_path_factory) -> Path:
    """A migrated database with a fresh install's row of it (what startup's her.seed
    makes), made once and copied for each test."""
    import sqlite3

    from celine import her
    p = tmp_path_factory.mktemp("template") / "celine.db"
    migrate_all(p)
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    her.seed(c)
    c.commit()
    c.close()
    return p


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """The test's database: var/celine.db, the one boot.load reads (_scratch_data_dir)."""
    return tmp_path / "var" / "celine.db"


@pytest.fixture
async def store(db_path: Path):
    import anyio

    async with anyio.create_task_group() as tg:
        s = Store(db_path)
        await s.start(tg)
        yield s
        await s.aclose()


def _probe_reply(request):
    """What an idle model server answers to the router's probes."""
    import httpx
    path = request.url.path
    if path.endswith("/running"):
        return httpx.Response(200, json={"running": []})
    if path.endswith("/api/ps"):
        return httpx.Response(200, json={"models": []})
    return httpx.Response(200, json={"data": []})


# the skills tests make up for their turns: always loaded, as the core skills are
# (turns/loads.py loads the real ones by need)
TEST_SKILLS = {"arch", "painter", "slow", "t", "door", "echo", "chatlink", "slowpoke"}


@pytest.fixture(autouse=True)
def _test_skills_always_loaded(monkeypatch):
    from celine.turns import loads
    monkeypatch.setattr(loads, "CORE", loads.CORE | TEST_SKILLS)


@pytest.fixture(autouse=True)
def _own_names(monkeypatch):
    """Each test starts with the names a fresh load gives, not the last test's."""
    from celine.intercom import store as rows
    monkeypatch.setattr(rows, "NAMES", {**rows.NAMES, "her": "Celine"})


@pytest.fixture(autouse=True)
def _scratch_data_dir(tmp_path, monkeypatch, _template_db):
    """No test reads or writes the real var/ (settings, keys, knowledge bases). Its
    database is migrated with its row in it, as after a start."""
    import shutil
    monkeypatch.setenv("CELINE_DATA_DIR", str(tmp_path / "var"))
    (tmp_path / "var").mkdir()
    shutil.copyfile(_template_db, tmp_path / "var" / "celine.db")
    shutil.copyfile(log_path(_template_db), tmp_path / "var" / "log.db")
    (tmp_path / "var" / "notebook").mkdir()
    shutil.copyfile(notebook_db(_template_db), tmp_path / "var" / "notebook" / ".index.db")
    # as after Setup saved the features: every feature on, shares too, which is on only
    # once saved on (unsaved, what needs a service is off: settings/features.py seed;
    # test_features.py covers that state)
    from celine.settings import files as settings_files
    settings_files.write(tmp_path / "var" / "settings", "features", {"shares": True})
    # what startup makes once before the first load (__main__.preflight)
    from celine import keys
    from celine.settings import seed
    keys.ensure(tmp_path / "var")
    seed.seed(tmp_path / "var" / "settings")


@pytest.fixture
def stack_key(tmp_path):
    """One of the stack's keys (celine/keys.py) with a known value for this test:
    `stack_key("shell", "k" * 24)`. The derived files (the hash) follow it."""
    from celine import keys

    def put(name: str, value: str) -> str:
        d = tmp_path / "var" / "keys"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.key").write_text(value + "\n")
        keys.ensure(tmp_path / "var")
        return value
    return put


@pytest.fixture(autouse=True)
def _offline_router(monkeypatch):
    """No test reaches a real model server through the router: probes without a
    transport of their own get an idle fake server. Caches of the process start empty."""
    import types

    import httpx

    from celine.backends import router as router_mod
    from celine.kb import embed as embed_mod
    from celine.turns import tokens

    def client(cls):
        def make(*a, transport=None, **kw):
            return cls(*a, transport=transport or (
                httpx.MockTransport(_probe_reply)), **kw)
        return make

    shim = types.SimpleNamespace(**{k: getattr(httpx, k) for k in dir(httpx)
                                    if not k.startswith("__")})
    shim.Client = client(httpx.Client)
    shim.AsyncClient = client(httpx.AsyncClient)
    monkeypatch.setattr(router_mod, "httpx", shim)
    from celine.settings import kinds   # discovery too (the ping, "Check provider")
    monkeypatch.setattr(kinds, "_client", lambda transport=None, timeout=8.0: client(
        httpx.AsyncClient)(timeout=timeout, transport=transport))
    kinds._cache.clear()
    router_mod._standalone.clear()
    embed_mod.forget_fingerprints()
    tokens.reset()
    yield
    router_mod._standalone.clear()
    embed_mod.forget_fingerprints()
    tokens.reset()


@pytest.fixture(autouse=True)
def _process_zone():
    """reload.apply moves the process clock to the app's zone; the next test gets its own."""
    import os
    import time
    before = os.environ.get("TZ")
    yield
    if os.environ.get("TZ") != before:
        if before is None:
            os.environ.pop("TZ")
        else:
            os.environ["TZ"] = before
        time.tzset()
