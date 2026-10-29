"""Plugins (celine/plugins.py, api/plugins.py): a folder with a plugin.toml is a feature;
the data dir's replaces the base one by id; off, nothing of it is imported, made or
answered; on at runtime its file, its route and its tool are there; off again its route
goes and its file stays; its form round-trips through /api/forms/<id>."""
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


async def test_the_form_round_trips_through_api_forms(tmp_path, monkeypatch, store):
    var = scratch(tmp_path, monkeypatch)
    _echo(var)
    cfg = boot.load()
    app = make_app(cfg, store)
    async with client(app) as c:
        r = (await c.get("/api/forms/echo")).json()
        assert r["values"] == {"prefix": ""} and r["saved"] is False
        assert [f["key"] for f in r["fields"]] == ["prefix", "token"]
        assert r["secrets"] == {"token": {"state": "not set", "set_at": None}}
        assert r["actions"] == [{"id": "check", "label": "Check"}], "never the code it runs"
        assert not (var / "settings" / "echo.toml").exists()
        put = await c.put("/api/forms/echo", json={"values": {"prefix": "> "}},
                          headers={"If-Match": r["version"]})
        assert put.status_code == 200 and put.json()["values"] == {"prefix": "> "}
        assert 'prefix = "> "' in (var / "settings" / "echo.toml").read_text()
        assert cfg.settings.plugins["echo"] == {"prefix": "> "}
        again = (await c.get("/api/forms/echo")).json()
        assert again["saved"] is True and again["version"] == put.json()["version"]
        assert (await c.get("/api/settings/echo")).status_code == 404, "no old route"
        assert (await c.get("/api/forms/nothing")).status_code == 404
        assert "celine_plugins.echo" not in sys.modules, "a form is read, never imported"


async def test_a_secret_is_set_kept_deleted_and_never_echoed(tmp_path, monkeypatch, store):
    var = scratch(tmp_path, monkeypatch)
    _echo(var)
    cfg = boot.load()
    app = make_app(cfg, store)
    secret = "s3cret-token-xyz"
    key = var / "secrets" / "echo.token.key"
    async with client(app) as c:
        put = await c.put("/api/forms/echo", json={"values": {}, "secrets": {"token": secret}})
        assert put.status_code == 200 and secret not in put.text
        assert put.json()["secrets"]["token"]["state"] == "set"
        assert key.read_text().strip() == secret and key.stat().st_mode & 0o077 == 0
        got = await c.get("/api/forms/echo")
        assert secret not in got.text and got.json()["secrets"]["token"]["state"] == "set"
        kept = await c.put("/api/forms/echo", json={"values": {"prefix": "a"}})
        assert kept.json()["secrets"]["token"]["state"] == "set" and key.is_file()
        gone = await c.put("/api/forms/echo", json={"values": {}, "secrets": {"token": None}})
        assert gone.json()["secrets"]["token"]["state"] == "not set" and not key.exists()
        bad = await c.put("/api/forms/echo", json={"values": {"token": secret}})
        assert bad.status_code == 400 and secret not in bad.text
        assert bad.json()["error"]["detail"]["field"] == "echo.token"
        bad = await c.put("/api/forms/echo", json={"values": {}, "secrets": {"prefix": "x"}})
        assert bad.json()["error"]["detail"]["field"] == "echo.prefix"


async def test_a_stale_version_conflicts_and_a_bad_value_lands_on_its_field(
        tmp_path, monkeypatch, store):
    var = scratch(tmp_path, monkeypatch)
    _echo(var)
    cfg = boot.load()
    app = make_app(cfg, store)
    async with client(app) as c:
        old = (await c.get("/api/forms/echo")).json()["version"]
        assert (await c.put("/api/forms/echo", json={"values": {"prefix": "a"}},
                            headers={"If-Match": old})).status_code == 200
        stale = await c.put("/api/forms/echo", json={"values": {"prefix": "b"}},
                            headers={"If-Match": old})
        assert stale.status_code == 412 and stale.json()["error"]["code"] == "version_conflict"
        assert cfg.settings.plugins["echo"] == {"prefix": "a"}
        # a secret changes the version too: a page that loaded before it is stale
        now = (await c.get("/api/forms/echo")).json()["version"]
        await c.put("/api/forms/echo", json={"values": {"prefix": "a"}, "secrets": {"token": "t"}})
        assert (await c.put("/api/forms/echo", json={"values": {}},
                            headers={"If-Match": now})).status_code == 412
        bad = await c.put("/api/forms/echo", json={"values": {"prefix": "x" * 21}})
        assert bad.status_code == 400
        assert bad.json()["error"]["detail"]["field"] == "echo.prefix"
        nope = await c.put("/api/forms/echo", json={"values": {"nope": 1}})
        assert nope.json()["error"]["detail"]["field"] == "echo.nope"


async def test_an_action_runs_only_while_its_plugin_runs(tmp_path, monkeypatch, store):
    var = scratch(tmp_path, monkeypatch)
    _echo(var)
    cfg = boot.load()
    app = make_app(cfg, store)
    async with client(app) as c:
        off = await c.post("/api/forms/echo/check")
        assert off.status_code == 409 and "Echo is not running." in off.text
        assert "celine_plugins.echo" not in sys.modules
        assert (await c.post("/api/forms/echo/nope")).status_code == 404
        plugins.get("echo").data = var / "plugins" / "echo"       # what its runner sets
        assert (await c.post("/api/forms/echo/check")).json() == {
            "ok": False, "sentence": "No token."}
        await c.put("/api/forms/echo", json={"values": {}, "secrets": {"token": "t"}})
        assert (await c.post("/api/forms/echo/check")).json()["ok"] is True


def test_a_bad_form_is_a_problem_and_no_plugin(tmp_path, monkeypatch):
    var = scratch(tmp_path, monkeypatch)
    d = _echo(var)
    (d / "settings.toml").write_text('[[fields]]\nkey = "x"\nlabel = "X"\ntype = "colour"\n')
    cfg = boot.load()
    assert plugins.get("echo") is None
    assert any(p.file == "plugins" and "no known type" in p.message for p in cfg.problems)


def test_the_schema_cleans_what_it_keeps():
    from celine.settings import form
    from celine.settings.files import Invalid
    fields = [{"key": "host", "label": "Host", "type": "text", "default": "", "word": True},
              {"key": "names", "label": "Names", "type": "list", "default": []},
              {"key": "wait", "label": "Wait", "type": "duration", "default": 5, "min": 1,
               "max": 60},
              {"key": "pin", "label": "Pin", "type": "secret"}]
    assert form.validate("x", fields, {"host": " a.b ", "names": ["a", " ", "b "],
                                       "wait": 7}) == {"host": "a.b", "names": ["a", "b"],
                                                       "wait": 7}
    assert form.validate("x", fields, {"host": "  "}) == {}, "blank goes back to the default"
    for data, field in (({"host": "a b"}, "x.host"), ({"wait": 61}, "x.wait"),
                        ({"names": "a"}, "x.names"), ({"pin": "1"}, "x.pin")):
        with pytest.raises(Invalid) as e:
            form.validate("x", fields, data)
        assert e.value.field == field
    assert form.values(fields, {}) == {"host": "", "names": [], "wait": 5}


async def test_a_broken_plugin_switched_on_keeps_the_app_up(tmp_path, monkeypatch, store):
    from celine.errors import Busy
    from celine.settings import files
    var = scratch(tmp_path, monkeypatch)
    d = _echo(var)
    (d / "skill.py").write_text("raise RuntimeError('broken on purpose')\n")
    files.write(var / "settings", "features", {"echo": True})
    cfg = boot.load()
    app = make_app(cfg, store)              # builds the registry, as the start does
    reg = app.state.services.registry
    assert "echo" not in reg.skills and not any(t.startswith("echo__") for t in reg.tools)
    assert any(p.file == "plugin echo" and "broken on purpose" in p.message and not p.fatal
               for p in cfg.problems)
    async with client(app) as c:            # and again after a save at runtime
        put = await c.put("/api/features", json={"features": {"echo": True, "web": False}})
        assert put.status_code == 200
        assert "echo" not in app.state.services.registry.skills
    with pytest.raises(Busy, match="Echo is not running."):
        plugins.get("echo").db


async def test_a_bad_guide_keeps_the_plugin_out(tmp_path, monkeypatch, store):
    from celine.settings import files
    from celine.skills import build, check_guides
    var = scratch(tmp_path, monkeypatch)
    d = _echo(var)
    (d / "guide.md").write_text("---\nname: other\ndescription: x\n---\nbody\n")
    files.write(var / "settings", "features", {"echo": True})
    cfg = boot.load()
    app = make_app(cfg, store)
    assert "echo" not in build(app.state.services).skills
    assert any(p.file == "plugin echo" and "name must be 'echo'" in p.message
               for p in cfg.problems)
    assert check_guides() is None, "a plugin's guide never refuses the start"


def test_a_closed_file_drops_its_readers_and_reads_again(tmp_path):
    from celine.kb.db import KBFile
    f = KBFile(tmp_path / "x.db", ECHO / "migrations")
    first = f._reader()
    assert f.read_sync(lambda c: c.execute("SELECT count(*) FROM heard").fetchone()[0]) == 0
    f.close()
    with pytest.raises(Exception, match="closed"):
        first.execute("SELECT 1")
    assert f.read_sync(lambda c: c.execute("SELECT count(*) FROM heard").fetchone()[0]) == 0
    assert f._reader() is not first
