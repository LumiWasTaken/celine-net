"""Plugins (celine/plugins.py, api/plugins.py): a folder with a plugin.toml is a feature;
the data dir's replaces the base one by id; off, nothing of it is imported, made or
answered; on at runtime its file, its route and its tool are there; off again its route
goes and its file stays; its form round-trips through /api/settings/<id>."""
from __future__ import annotations

import functools
import shutil
import sys
from pathlib import Path

import anyio
import pytest

from celine import boot, plugins
from celine.api import plugins as plugins_api
from celine.core.runners import Runner, Runners
from celine.settings import features
from chat_fixtures import until
from settings_fixtures import client, make_app, scratch

pytestmark = pytest.mark.anyio

ECHO = Path(__file__).parent / "data" / "plugins" / "echo"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    """No base plugin but what a test puts there; no plugin module left behind."""
    monkeypatch.setattr(plugins, "BASE", tmp_path / "base")
    yield
    plugins.LOADED.clear()
    monkeypatch.setattr(plugins, "_scanned", None)
    for name in [m for m in sys.modules if m.split(".")[0] == plugins.PACKAGE]:
        del sys.modules[name]


def _echo(var: Path) -> Path:
    d = var / "plugins" / "echo"
    shutil.copytree(ECHO, d)
    return d


def test_scan_reads_the_base_and_the_data_dir_replaces_by_id(tmp_path, monkeypatch):
    base = tmp_path / "base"
    shutil.copytree(ECHO, base / "echo")
    var = scratch(tmp_path, monkeypatch)
    boot.load()
    p = plugins.get("echo")
    assert p.dir == base / "echo" and not p.user and p.form["fields"][0]["key"] == "prefix"
    assert "echo" in features.ids() and features.skill_owners()["echo"] == "echo"
    assert features.get("echo").start_on is False and features.get("echo").pages == ("echo",)
    mine = _echo(var)
    (mine / "plugin.toml").write_text((mine / "plugin.toml").read_text().replace(
        'label = "Echo"', 'label = "My echo"'))
    assert plugins.scan(var, frozenset()) == [
        "plugins changed on disk; restart Celine to apply it"]
    assert plugins.get("echo") is p, "a reload keeps the running record"
    monkeypatch.setattr(plugins, "_scanned", None)       # a restart
    plugins.scan(var, frozenset())
    assert plugins.get("echo").dir == mine and plugins.get("echo").user
    assert features.get("echo").label == "My echo"


def test_a_bad_manifest_is_a_problem_and_no_plugin(tmp_path, monkeypatch):
    var = scratch(tmp_path, monkeypatch)
    bad = var / "plugins" / "general"
    bad.mkdir(parents=True)
    (bad / "plugin.toml").write_text('id = "general"\nlabel = "Taken"\n')
    cfg = boot.load()
    assert plugins.get("general") is None
    assert any(p.file == "plugins" and "cannot be a plugin id" in p.message
               for p in cfg.problems)


async def test_off_on_off_at_runtime(tmp_path, monkeypatch, store):
    var = scratch(tmp_path, monkeypatch)
    _echo(var)
    cfg = boot.load()
    app = make_app(cfg, store)
    mount, routes = plugins_api.mount()
    app.router.routes.append(mount)
    services = app.state.services
    db = var / "plugins" / "echo" / "echo.db"
    assert "celine_plugins.echo" not in sys.modules
    assert "echo" not in services.registry.skills
    async with anyio.create_task_group() as tg, client(app) as c:
        runners = services.runners = Runners()
        p = plugins.get("echo")
        runners["echo"] = Runner(tg, functools.partial(plugins_api.run, services, routes, p))
        await services.runners.apply(features.effective(cfg.settings.features))
        r = await c.get("/api/plugins/echo/heard")
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
        assert not db.exists() and "celine_plugins.echo" not in sys.modules

        await c.put("/api/features", json={"features": {"echo": True}})
        await until(lambda: p.file is not None and len(routes.routes) == 1)
        assert db.is_file() and "celine_plugins.echo" in sys.modules
        spec = services.registry.tools["echo__say"]
        assert (await spec.fn(None, text="hi")).content == "hi"
        assert (await c.get("/api/plugins/echo/heard")).json() == {"count": 1}

        await c.put("/api/features", json={"features": {"echo": False}})
        await until(lambda: not runners["echo"].running)
        assert routes.routes == [] and p.file is None and db.is_file()
        assert (await c.get("/api/plugins/echo/heard")).status_code == 404
        assert "echo__say" not in services.registry.tools
        tg.cancel_scope.cancel()


async def test_the_form_round_trips(tmp_path, monkeypatch, store):
    var = scratch(tmp_path, monkeypatch)
    _echo(var)
    cfg = boot.load()
    app = make_app(cfg, store)
    async with client(app) as c:
        r = (await c.get("/api/settings/echo")).json()
        assert r["values"] == {"prefix": ""} and r["saved"] is False
        assert [f["key"] for f in r["fields"]] == ["prefix"]
        assert not (var / "settings" / "echo.toml").exists()
        put = await c.put("/api/settings/echo", json={"values": {"prefix": "> "}})
        assert put.status_code == 200 and put.json()["values"] == {"prefix": "> "}
        assert 'prefix = "> "' in (var / "settings" / "echo.toml").read_text()
        assert cfg.settings.plugins["echo"] == {"prefix": "> "}
        again = (await c.get("/api/settings/echo")).json()
        assert again["saved"] is True and again["version"] == put.json()["version"]
        bad = await c.put("/api/settings/echo", json={"values": {"prefix": "x" * 21}})
        assert bad.status_code == 400
        assert bad.json()["error"]["detail"]["field"] == "echo.prefix"
        assert (await c.put("/api/settings/echo", json={"values": {"nope": 1}})).status_code == 400
        assert (await c.get("/api/settings/nothing")).status_code == 404
        assert "celine_plugins.echo" not in sys.modules, "a form is read, never imported"
