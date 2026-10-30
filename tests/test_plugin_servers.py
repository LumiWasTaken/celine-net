"""A plugin's [servers] (mcp.plugin_servers, api/plugins.py): each `${KEY}` filled from its
form, an empty value keeps that server down with the reason; on, its server's tools are
there with its plugin's guide; a form save starts it again with the new values; off, its
tools go. The mac plugin: the setup script and the report the Mac posts back (with the
link key), her scripts' messages to her, the setup line on its form and in her
settings__read, and Check's lines."""
from __future__ import annotations

import functools
import json
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from celine import boot, plugins, saves
from celine.api import plugins as plugins_api
from celine.core.runners import Runner, Runners
from celine.devices import about, devices
from celine.mcp import Supervisor, plugin_servers
from celine.settings import features, form
from celine.conversations import store as conversations
from celine.skills import ToolContext
from celine.skills.settings import Settings as SettingsSkill, _copies
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
              "agents": "http://127.0.0.1:4096",
              "servers": {"screen": {"tools": 26, "missing": []}},
              "mac": {"user": "celine", "home": "/Users/celine", "chip": "Apple M4",
                      "memory": 17179869184, "macos": "15.6", "network": "Wi-Fi",
                      "interface": "en0"}}
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

        gateway = sys.modules[p.attr("gateway:copies").__module__]
        key = form.read_secret(cfg, "mac", "link")
        # the app only on localhost: no line, what to do first and the app's address field
        view = (await c.get("/api/forms/mac")).json()
        assert view["note"] == gateway.LOCAL and view["address"] is True
        assert [f["key"] for f in view["fields"]] == [], "the Mac fills every other field"
        assert view["values"] == {} and view["secrets"] == {}
        assert await _copies(services, p) == {"note": gateway.LOCAL}, "no field for her"
        # on the network: one line, a token that works once, the key never in the line
        await saves.address(services, {"origin": "http://192.168.1.20:8766"})
        view = (await c.get("/api/forms/mac")).json()
        command = next(f for f in view["fields"] if f["key"] == "command")["value"]
        m = re.fullmatch(r"curl -fsSL http://192\.168\.1\.20:8766/api/plugins/mac/setup/"
                         r"([\w-]{40,}) \| bash", command)
        assert m and key not in command and "address" not in view
        assert view["note"] == gateway.WAITING
        assert plugins.is_open("GET", f"/api/plugins/mac/setup/{m[1]}")
        assert not plugins.is_open("GET", "/api/plugins/mac/setup/")
        served = await c.get(f"/api/plugins/mac/setup/{m[1]}")
        assert served.status_code == 200
        assert f"APP=http://192.168.1.20:8766 KEY={key} JSON=''" in served.text
        assert gateway.PRESET not in served.text and served.text.startswith("#!/bin/bash")
        assert (await c.get(f"/api/plugins/mac/setup/{m[1]}")).status_code == 404, "once"
        # an hour later it is gone
        late = (await _copies(services, p))["copy"]["Run on the Mac"].split("/")[-1].split()[0]
        gateway._tokens[late] -= gateway.SETUP_SECONDS + 1
        assert (await c.get(f"/api/plugins/mac/setup/{late}")).status_code == 404
        # a served copy always has the app and the key in it
        with pytest.raises(RuntimeError):
            gateway.filled("http://192.168.1.20:8766", "")
        script, gateway.SCRIPT = gateway.SCRIPT, tmp_path / "no-preset.sh"
        gateway.SCRIPT.write_text("#!/bin/bash\nAPP='' KEY=''\n")
        try:
            with pytest.raises(RuntimeError):
                gateway.filled("http://192.168.1.20:8766", key)
        finally:
            gateway.SCRIPT = script
        # her settings__read: the line in her own chat, not in a sub-agent's
        sk = SettingsSkill(cfg)
        sk.app = services

        async def read_in(cid):
            ctx = ToolContext(conversation_id=cid, turn_id="t", tool_call_id="k", store=store,
                              config=cfg, emit=None)  # type: ignore[arg-type]
            return json.loads((await sk.read(ctx, target="forms.mac")).content)
        chat = await conversations.create(store)
        agent = await conversations.create(store, agent_of=chat, agent_name="a", kind="agent")
        assert (await read_in(chat))["copy"]["Run on the Mac"].startswith("curl -fsSL ")
        ctx = ToolContext(conversation_id=chat, turn_id="t", tool_call_id="k", store=store,
                          config=cfg, emit=None)  # type: ignore[arg-type]
        no = await sk.propose(ctx, target="forms.mac", changes={"url": "http://x:8765"}, why="t")
        assert no.is_error and no.content.startswith("Not a setting of forms.mac: url.")
        assert "copy" not in await read_in(agent) and "note" not in await read_in(agent)
        assert devices(cfg)["mac"].why == "Her Mac: Run the setup line on the Mac."
        assert about(cfg) is None
        before = next(f for f in (await c.get("/api/features")).json()["catalog"]
                      if f["id"] == "mac")
        assert before["state"] == "unset"
        from celine.settings import write
        seen, publish = [], write.publish
        monkeypatch.setattr(write, "publish", lambda s, what: (
            seen.append((what, s.feature_probes)), publish(s, what)))

        wrong = await c.post("/api/plugins/mac/gateway", json=report,
                             headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 404
        ok = await c.post("/api/plugins/mac/gateway", json=report,
                          headers={"Authorization": f"Bearer {key}"})
        assert ok.status_code == 200
        view = (await c.get("/api/forms/mac")).json()
        saved = cfg.settings.plugins["mac"]
        assert saved["url"] == "http://127.0.0.1:9"
        assert form.read_secret(cfg, "mac", "token") == "mac-token-1"
        assert "mac-token-1" not in str(view) and "127.0.0.1:9" not in str(view)
        assert saved["agents"] == "http://127.0.0.1:4096"
        facts = ("user celine, home /Users/celine, Apple M4, 16 GB memory, macOS 15.6, "
                 "Wi-Fi (en0)")
        assert await _copies(services, p) == {"note": f"Connected: {facts}."}, "no line"
        assert view["note"] == f"Connected: {facts}." and view["fields"] == []
        # a client's save keeps what the Mac filled and may not name it
        bad = await c.put("/api/forms/mac", headers={"If-Match": view["version"]},
                          json={"values": {"url": "http://x:1"}})
        assert bad.status_code == 400 and bad.json()["error"]["detail"]["field"] == "mac.url"
        bad = await c.put("/api/forms/mac", headers={"If-Match": view["version"]},
                          json={"values": {}, "secrets": {"token": "x"}})
        assert bad.status_code == 400 and bad.json()["error"]["detail"]["field"] == "mac.token"
        ok = await c.put("/api/forms/mac", headers={"If-Match": view["version"]},
                         json={"values": {}})
        assert ok.status_code == 200 and cfg.settings.plugins["mac"]["url"] == saved["url"]
        # Set up again: the line until the Mac reports anew
        assert (await c.post("/api/forms/mac/again")).json()["ok"] is True
        view = (await c.get("/api/forms/mac")).json()
        assert view["note"] == gateway.WAITING
        assert any(f["key"] == "command" for f in view["fields"])
        assert (await c.post("/api/plugins/mac/gateway", json=report,
                             headers={"Authorization": f"Bearer {key}"})).status_code == 200
        assert (await c.get("/api/forms/mac")).json()["note"] == f"Connected: {facts}."
        after = next(f for f in (await c.get("/api/features")).json()["catalog"]
                     if f["id"] == "mac")
        assert after["state"] != "unset", "the save asks the probes anew"
        assert seen and all(x == (["mac"], None) for x in seen), \
            "a page that reloads on the event reads new states"
        assert cfg.settings.plugins["mac"]["about"] == facts
        assert about(cfg) == f"Her Mac: {facts}."
        mac = devices(cfg)["mac"]      # the drones' device: its password the token, its key the link
        assert (mac.url, mac.password, mac.key, mac.why) == (
            "http://127.0.0.1:4096", "mac-token-1", key, None)
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
    assert out["lines"][1] == {"text": "Shell: Run the setup line on the Mac.", "tone": "error"}


async def test_a_fresh_install_switches_her_mac_on_while_it_is_not_set_up(
        tmp_path, monkeypatch, store):
    """Its form fills from what runs only while it is on: not set up never blocks it."""
    var = scratch(tmp_path, monkeypatch)
    (var / "settings" / "features.toml").unlink(missing_ok=True)
    cfg = boot.load()
    app, services, routes = _app(cfg, store)
    p = plugins.get("mac")
    async with anyio.create_task_group() as tg, client(app) as c:
        mac = next(f for f in (await c.get("/api/features")).json()["catalog"]
                   if f["id"] == "mac")
        assert (mac["state"], mac["blocks"]) == ("unset", False)
        runners = services.runners = Runners()
        runners["mac"] = Runner(tg, functools.partial(plugins_api.run, services, routes, p))
        await c.put("/api/features", json={"features": {"mac": True}})
        await until(lambda: p.data is not None)
        await saves.address(services, {"origin": "http://192.168.1.20:8766"})
        view = (await c.get("/api/forms/mac")).json()
        command = next(f for f in view["fields"] if f["key"] == "command")
        assert command["label"] == "Run on the Mac"
        token = command["value"].split("/setup/")[1].split()[0]
        assert (await c.get(f"/api/plugins/mac/setup/{token}")).status_code == 200
        got = (await c.get("/api/features")).json()
        mac = next(f for f in got["catalog"] if f["id"] == "mac")
        assert got["features"]["mac"] is True
        assert (mac["state"], mac["blocks"]) == ("unset", False)
        assert mac["detail"] == "Run the setup line on the Mac."
        tg.cancel_scope.cancel()
