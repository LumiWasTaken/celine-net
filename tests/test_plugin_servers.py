"""A plugin's [servers] (mcp.plugin_servers, api/plugins.py): each `${KEY}` filled from its
form, an empty value keeps that server down with the reason; on, its server's tools are
there with its plugin's guide; a form save starts it again with the new values; off, its
tools go. The mac plugin: the setup script and the report the Mac posts back (with the
link key), her scripts' messages to her, the command on its form, and Check's lines."""
from __future__ import annotations

import functools
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from celine import boot, plugins
from celine.api import plugins as plugins_api
from celine.core.runners import Runner, Runners
from celine.mcp import Supervisor, plugin_servers
from celine.settings import features, form
from chat_fixtures import until
from settings_fixtures import client, make_app, scratch

pytestmark = pytest.mark.anyio

REMOTE = Path(__file__).parent / "data" / "plugins" / "remote"
SERVER = Path(__file__).with_name("mcp_echo_server.py")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    yield
    plugins.LOADED.clear()
    monkeypatch.setattr(plugins, "_scanned", None)
    for name in [m for m in sys.modules if m.split(".")[0] == plugins.PACKAGE]:
        del sys.modules[name]


def _app(cfg, store):
    """The test app with the plugins' mount and a supervisor."""
    app = make_app(cfg, store)
    mount, routes = plugins_api.mount()
    app.router.routes.append(mount)
    services = app.state.services
    services.mcp = Supervisor(cfg, store, services.registry, ping_seconds=0.5)
    return app, services, routes


def test_each_key_is_filled_from_the_form_and_an_empty_one_says_why(tmp_path, monkeypatch):
    var = scratch(tmp_path, monkeypatch)
    shutil.copytree(REMOTE, var / "plugins" / "remote")
    cfg = boot.load()
    p = plugins.get("remote")
    assert features.connector_owners()["remote_echo"] == "remote"
    assert plugin_servers(cfg, p) == ({}, {"remote_echo": "Python is empty; Script is empty; "
                                                         "Token is empty."})
    cfg.settings.plugins["remote"] = {"python": sys.executable, "script": str(SERVER)}
    assert plugin_servers(cfg, p)[1] == {"remote_echo": "Token is empty."}
    form.secrets.set_key(cfg.settings.secrets_root, "remote.token", "t0k3n-one")
    servers, why = plugin_servers(cfg, p)
    s = servers["remote_echo"]
    assert why == {} and s.command == sys.executable and s.args == [str(SERVER)]
    assert s.env == {"ECHO_TOKEN": "t0k3n-one"} and s.sends == "house" and s.enabled


async def test_on_its_server_runs_a_save_starts_it_again_off_its_tools_go(
        tmp_path, monkeypatch, store):
    var = scratch(tmp_path, monkeypatch)
    shutil.copytree(REMOTE, var / "plugins" / "remote")
    cfg = boot.load()
    app, services, routes = _app(cfg, store)
    reg = services.registry
    p = plugins.get("remote")
    async with anyio.create_task_group() as tg, client(app) as c:
        runners = services.runners = Runners()
        runners["remote"] = Runner(tg, functools.partial(plugins_api.run, services, routes, p))
        await c.put("/api/features", json={"features": {"remote": True}})
        await until(lambda: p.data is not None)
        assert "remote_echo" not in services.mcp.skills, "an empty value starts nothing"

        version = (await c.get("/api/forms/remote")).json()["version"]
        r = await c.put("/api/forms/remote", headers={"If-Match": version}, json={
            "values": {"python": sys.executable, "script": str(SERVER)},
            "secrets": {"token": "first"}})
        assert r.status_code == 200
        await until(lambda: "remote_echo__env" in reg.tools
                    and services.mcp.skills["remote_echo"].status == "up")
        skill = services.mcp.skills["remote_echo"]
        assert skill.description == "The echo server of the Remote plugin."
        assert (await reg.tools["remote_echo__env"].fn(None, name="ECHO_TOKEN")).content == "first"

        version = (await c.get("/api/forms/remote")).json()["version"]
        await c.put("/api/forms/remote", headers={"If-Match": version}, json={
            "values": {"python": sys.executable, "script": str(SERVER)},
            "secrets": {"token": "second"}})
        await until(lambda: services.mcp.skills.get("remote_echo") not in (None, skill)
                    and services.mcp.skills["remote_echo"].status == "up")
        assert (await reg.tools["remote_echo__env"].fn(None, name="ECHO_TOKEN")).content == \
            "second"

        await c.put("/api/features", json={"features": {"remote": False}})
        await until(lambda: not runners["remote"].running)
        assert "remote_echo" not in services.mcp.skills
        assert not any(t.skill == "remote_echo" for t in reg.tools.values())
        tg.cancel_scope.cancel()


async def test_the_mac_posts_its_report_with_the_link_key(tmp_path, monkeypatch, store):
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    app, services, routes = _app(cfg, store)
    p = plugins.get("mac")
    report = {"address": "http://127.0.0.1:9", "token": "mac-token-1", "token_file": "/x",
              "servers": {"screen": {"tools": 26, "missing": []}}}
    async with anyio.create_task_group() as tg, client(app) as c:
        assert not plugins.is_open("POST", "/api/plugins/mac/gateway"), "off: nothing is open"
        runners = services.runners = Runners()
        runners["mac"] = Runner(tg, functools.partial(plugins_api.run, services, routes, p))
        await c.put("/api/features", json={"features": {"mac": True}})
        await until(lambda: p.data is not None)
        assert plugins.is_open("POST", "/api/plugins/mac/gateway")
        assert plugins.is_open("GET", "/api/plugins/mac/setup.sh")
        assert not plugins.is_open("GET", "/api/plugins/mac/gateway")

        script = await c.get("/api/plugins/mac/setup.sh")
        assert script.status_code == 200 and script.text.startswith("#!/bin/bash")

        view = (await c.get("/api/forms/mac")).json()
        key = form.read_secret(cfg, "mac", "link")
        command = next(f for f in view["fields"] if f["key"] == "command")["value"]
        assert command == ("curl -fsSLo setup.sh http://t/api/plugins/mac/setup.sh && "
                           f"bash setup.sh --app http://t --key {key}")
        assert view["values"]["url"] == "" and view["secrets"]["token"]["state"] == "not set"

        wrong = await c.post("/api/plugins/mac/gateway", json=report,
                             headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 404
        ok = await c.post("/api/plugins/mac/gateway", json=report,
                          headers={"Authorization": f"Bearer {key}"})
        assert ok.status_code == 200
        view = (await c.get("/api/forms/mac")).json()
        assert view["values"]["url"] == "http://127.0.0.1:9"
        assert view["secrets"]["token"]["state"] == "set"
        assert form.read_secret(cfg, "mac", "token") == "mac-token-1"
        assert "mac-token-1" not in str(view)
        await until(lambda: set(services.mcp.skills) == {"macscreen", "macshell", "macbrowser",
                                                         "macfiles"})
        assert services.mcp.skills["macshell"].cfg.url == "http://127.0.0.1:9/shell/mcp"
        assert services.mcp.skills["macshell"].cfg.headers == {
            "Authorization": "Bearer mac-token-1"}
        assert services.mcp.skills["macshell"].guide.description.startswith(
            "A shell on your own Mac")
        tg.cancel_scope.cancel()


async def test_her_script_on_the_mac_starts_her_turn_with_a_message_to_herself(
        tmp_path, monkeypatch, store):
    from celine.conversations import store as conversations
    from celine.intercom import Intercom
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    app, services, routes = _app(cfg, store)
    services.intercom = Intercom(store)
    p = plugins.get("mac")
    older, newest = await conversations.create(store), await conversations.create(store)
    await store.write(lambda c: c.execute(
        "UPDATE conversations SET updated_at='2099-01-01T00:00:00Z' WHERE id=?", (newest,)))
    async with anyio.create_task_group() as tg, client(app) as c:
        runners = services.runners = Runners()
        runners["mac"] = Runner(tg, functools.partial(plugins_api.run, services, routes, p))
        await c.put("/api/features", json={"features": {"mac": True}})
        await until(lambda: p.data is not None)
        assert plugins.is_open("POST", "/api/plugins/mac/events")
        key = form.read_secret(cfg, "mac", "link")
        auth = {"Authorization": f"Bearer {key}"}

        wrong = await c.post("/api/plugins/mac/events", json={"text": "hi"},
                             headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 404
        out = (await c.post("/api/plugins/mac/events", json={"text": "the build is done"},
                            headers=auth)).json()
        assert out["conversation"] == newest and out["turn_id"], "her newest chat, her turn"
        named = (await c.post("/api/plugins/mac/events",
                              json={"text": "backup done", "conversation": older},
                              headers=auth)).json()
        assert named["conversation"] == older and named["turn_id"]
        rows = await store.read(lambda c: [tuple(r) for r in c.execute(
            "SELECT from_addr, to_addr, text FROM intercom ORDER BY id")])
        assert rows == [(f"agent:{newest}", f"agent:{newest}", "the build is done"),
                        (f"agent:{older}", f"agent:{older}", "backup done")]
        gone = await c.post("/api/plugins/mac/events",
                            json={"text": "x", "conversation": "nope"}, headers=auth)
        assert gone.status_code == 404
        empty = await c.post("/api/plugins/mac/events", json={"text": " "}, headers=auth)
        assert empty.status_code == 400
        tg.cancel_scope.cancel()


# Peekaboo's permissions answer on a Mac without the grants, as it came (2026-10-29)
PERMISSIONS = (
    "macOS Permissions Status:\n\nScreen Recording (Required): [err] Not Granted\n"
    "Accessibility (Required): [err] Not Granted\nEvent Synthesizing (Action-specific): "
    "[warn] Not Granted\n\n[warn] Screen Recording permission is REQUIRED for capturing "
    "screenshots.\nGrant via: System Settings > Privacy & Security > Screen & System Audio "
    "Recording\n\n[warn] Accessibility permission is REQUIRED for UI automation.\nGrant via: "
    "System Settings > Privacy & Security > Accessibility\n\nEvent Synthesizing is not "
    "globally required, but these actions are unavailable: background keyboard input, "
    "foreground synthetic pointer input.\nBackground Accessibility actions remain available.")
META = {"accessibility": False, "event_synthesizing": False,
        "event_synthesizing_limits": ["background keyboard input"],
        "permission_snapshot_available": True, "required_permissions_granted": False,
        "screen_recording": False}


async def test_check_lists_each_server_and_each_grant_the_screen_lacks(tmp_path, monkeypatch):
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    p = plugins.get("mac")
    cfg.settings.plugins["mac"] = {"url": "http://mac:8765"}
    form.secrets.set_key(cfg.settings.secrets_root, "mac.token", "tok")

    class Client:
        async def call_tool(self, name, args):
            assert name == "permissions"
            return SimpleNamespace(meta=META, content=[SimpleNamespace(text=PERMISSIONS)])

    skills = {"macscreen": SimpleNamespace(status="up", error=None, client=Client()),
              "macshell": SimpleNamespace(status="down", client=None,
                                          error="The macshell server did not answer at "
                                                "mac:8765. Start it, or fix the address.")}
    tools = {f"macscreen__t{i}": SimpleNamespace(skill="macscreen") for i in range(26)}
    services = SimpleNamespace(cfg=cfg, mcp=SimpleNamespace(skills=skills),
                               registry=SimpleNamespace(tools=tools))
    out = await p.attr("gateway:check")(services, p, None)
    assert out == {"ok": False, "sentence": "", "lines": [
        {"text": "Screen: 26 tools", "tone": "ok"},
        {"text": "Shell: The macshell server did not answer at mac:8765. Start it, or fix "
                 "the address.", "tone": "error"},
        {"text": "Browser: starting", "tone": "error"},
        {"text": "Files: starting", "tone": "error"},
        {"text": "Accessibility (Required): Not Granted. Grant via: System Settings > "
                 "Privacy & Security > Accessibility", "tone": "error"},
        {"text": "Event Synthesizing (Action-specific): Not Granted", "tone": "warn"},
        {"text": "Screen Recording (Required): Not Granted. Grant via: System Settings > "
                 "Privacy & Security > Screen & System Audio Recording", "tone": "error"}]}

    cfg.settings.plugins["mac"] = {}
    out = await p.attr("gateway:check")(services, p, None)
    assert out["lines"][1] == {"text": "Shell: Address is empty.", "tone": "error"}
