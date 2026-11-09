"""A plugin's [servers] (mcp.plugin_servers, api/plugins.py): each `${KEY}` filled from its
form, an empty value keeps that server down with the reason; on, its server's tools are
there with its plugin's guide; a form save starts it again with the new values; off, its
tools go. The mac plugin: the setup script and the report the Mac posts back (with the
link key), its scripts' messages to the agent, the setup command on its form and in its
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
import httpx
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
              "agents": "http://127.0.0.1:4096", "opencode": {"version": "1.19.4"},
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
        lock = (await c.get("/api/plugins/mac/package-lock.json")).json()
        wanted = (await c.get("/api/plugins/mac/package.json")).json()["dependencies"]
        assert lock["packages"][""]["dependencies"] == wanted, "npm ci needs the two in step"
        assert all(v.get("integrity") for k, v in lock["packages"].items() if k), "every sum"

        gateway = sys.modules[p.attr("gateway:copies").__module__]
        key = form.read_secret(cfg, "mac", "link")
        # the app only on localhost: no line, what to do first and the app's address field
        view = (await c.get("/api/forms/mac")).json()
        assert view["note"] == gateway.LOCAL and view["address"] is True
        assert [f["key"] for f in view["fields"]] == ["publish"], "the Mac fills every other field"
        assert view["fields"][0]["value"] == gateway.PUBLISH
        assert [a["id"] for a in view["actions"]] == ["check"], \
            "Set up again and Update only once connected"
        assert view["values"] == {} and view["secrets"] == {}
        assert await _copies(services, p) == {
            "note": gateway.LOCAL,
            "copy": {"Run in a terminal in the Celine folder on this computer": gateway.PUBLISH}}, "the line to publish, for the agent too"
        # on the network: one line, a token that works once, the key never in the line
        await saves.address(services, {"origin": "http://192.168.1.20:8766"})
        view = (await c.get("/api/forms/mac")).json()
        command = next(f for f in view["fields"] if f["key"] == "command")["value"]
        m = re.fullmatch(r"curl -fsSL http://192\.168\.1\.20:8766/api/plugins/mac/setup/"
                         r"([\w-]{40,}) \| bash", command)
        assert m and key not in command and "address" not in view
        assert view["note"] == gateway.WAITING
        assert [a["id"] for a in view["actions"]] == ["check"]
        assert plugins.is_open("GET", f"/api/plugins/mac/setup/{m[1]}")
        assert not plugins.is_open("GET", "/api/plugins/mac/setup/")
        served = await c.get(f"/api/plugins/mac/setup/{m[1]}")
        assert served.status_code == 200
        assert (f"APP=http://192.168.1.20:8766 KEY={key} SETUP={gateway.version()} JSON=''"
                in served.text), "the setup's version, which its report says back"
        assert gateway.PRESET not in served.text and served.text.startswith("#!/bin/bash")
        assert (await c.get(f"/api/plugins/mac/setup/{m[1]}")).status_code == 404, "once"
        # an hour later it is gone
        late = (await _copies(services, p))["copy"]["Run in Terminal on the Mac"].split("/")[-1].split()[0]
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
        # its settings__read: the line in its own chat, not in a sub-agent's
        sk = SettingsSkill(cfg)
        sk.app = services

        async def read_in(cid):
            ctx = ToolContext(conversation_id=cid, turn_id="t", tool_call_id="k", store=store,
                              config=cfg, emit=None)  # type: ignore[arg-type]
            return json.loads((await sk.read(ctx, target="forms.mac")).content)
        chat = await conversations.create(store)
        agent = await conversations.create(store, agent_of=chat, agent_name="a", kind="agent")
        assert (await read_in(chat))["copy"]["Run in Terminal on the Mac"].startswith("curl -fsSL ")
        ctx = ToolContext(conversation_id=chat, turn_id="t", tool_call_id="k", store=store,
                          config=cfg, emit=None)  # type: ignore[arg-type]
        no = await sk.propose(ctx, target="forms.mac", changes={"url": "http://x:8765"}, why="t")
        assert no.is_error and no.content.startswith("Not a setting of forms.mac: url.")
        assert "copy" not in await read_in(agent) and "note" not in await read_in(agent)
        assert devices(cfg)["mac"].why == "Celine's Mac: The Mac is not connected yet."
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
        shown = [{"label": "Chip", "value": "Apple M4"}, {"label": "Memory", "value": "16 GB"},
                 {"label": "macOS", "value": "15.6"}, {"label": "Network", "value": "Wi-Fi (en0)"},
                 {"label": "User", "value": "celine"}, {"label": "opencode", "value": "1.19.4"}]
        assert await _copies(services, p) == {
            "note": "Connected. Chip: Apple M4. Memory: 16 GB. macOS: 15.6. Network: Wi-Fi (en0). "
                    "User: celine. opencode: 1.19.4."}, "the facts as sentences for the agent"
        assert view["note"] == "Connected." and view["facts"] == shown and view["fields"] == []
        # a Mac that said no setup version (set up by hand, or before versions) has an update
        assert view["actions"] == [
            {"id": "check", "label": "Check"},
            {"id": "update", "label": "Update", "badge": "Update available"},
            {"id": "again", "label": "Set up again"}]
        for said, badge in ((gateway.version(), None), ("0123456789ab", "Update available")):
            assert (await c.post("/api/plugins/mac/gateway", json={**report, "setup": said},
                                 headers={"Authorization": f"Bearer {key}"})).status_code == 200
            assert cfg.settings.plugins["mac"]["setup"] == said
            actions = (await c.get("/api/forms/mac")).json()["actions"]
            assert [a.get("badge") for a in actions] == [None, badge, None], said
        # a report where opencode did not answer shows no version
        silent = {**report, "opencode": {"version": None, "error": "no answer"}}
        assert (await c.post("/api/plugins/mac/gateway", json=silent,
                             headers={"Authorization": f"Bearer {key}"})).status_code == 200
        assert "opencode" not in cfg.settings.plugins["mac"]
        assert (await c.post("/api/plugins/mac/gateway", json=report,
                             headers={"Authorization": f"Bearer {key}"})).status_code == 200
        view = (await c.get("/api/forms/mac")).json()
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
        assert (await c.get("/api/forms/mac")).json()["facts"] == shown
        after = next(f for f in (await c.get("/api/features")).json()["catalog"]
                     if f["id"] == "mac")
        assert after["state"] != "unset", "the save asks the probes anew"
        assert seen and all(x == (["mac"], None) for x in seen), \
            "a page that reloads on the event reads new states"
        assert cfg.settings.plugins["mac"]["about"] == facts
        assert about(cfg) == f"Celine's Mac: {facts}."
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


async def test_the_agents_script_on_the_mac_starts_its_turn_with_a_message_to_itself(
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
        assert out["conversation"] == newest and out["turn_id"], "its newest chat, its turn"
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


# Peekaboo's permissions answer's _meta on a Mac without the grants, as it came (2026-10-29)
UPDATE = {"update": "Update available"}   # copies' badge while the Mac said no setup version
META ={"accessibility": False, "event_synthesizing": False,
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
        meta = META

        async def call_tool(self, name, args):
            assert name == "permissions"
            return SimpleNamespace(meta=self.meta, content=[])

    skills = {"macscreen": SimpleNamespace(status="up", error=None, client=Client()),
              "macshell": SimpleNamespace(status="down", client=None,
                                          error="The macshell server did not answer at "
                                                "mac:8765. Make sure it is running and the address is right, then press Check.")}
    tools = {f"macscreen__t{i}": SimpleNamespace(skill="macscreen") for i in range(26)}
    services = SimpleNamespace(cfg=cfg, mcp=SimpleNamespace(skills=skills),
                               registry=SimpleNamespace(tools=tools))
    out = await p.attr("gateway:check")(services, p, None)
    turn_on = ("On the Mac, open System Settings, Privacy & Security, and turn on Celine under "
               "Screen & System Audio Recording and Accessibility.")
    assert out == {"ok": False, "sentence": turn_on, "lines": [
        {"text": "Screen: 26 tools", "tone": "ok"},
        {"text": "Shell: The macshell server did not answer at mac:8765. Make sure it is running "
                 "and the address is right, then press Check.", "tone": "error"},
        {"text": "Browser: Starting", "tone": "error"},
        {"text": "Files: Starting", "tone": "error"},
        {"text": "Screen Recording is off: no screenshots.", "tone": "error"},
        {"text": "Accessibility is off: no clicks or typing.", "tone": "error"},
        {"text": "Event Synthesizing is off: no typing into apps in the background.",
         "tone": "warn"}]}
    # a server whose calls would wait on a dialog is blocked with the reason (skills.BLOCKED)
    from celine.skills import BLOCKED
    assert BLOCKED == {"macscreen": "Screen Recording and Accessibility are off on the Mac. "
                                    + turn_on}
    # the form's note and its settings__read say the same while the Mac is connected
    form.secrets.set_key(cfg.settings.secrets_root, "mac.link", "key")
    copies = p.attr("gateway:copies")
    got = await copies(services, p)
    assert got["note"] == f"Connected. {turn_on}", "the lists named once"
    assert got["lines"] == out["lines"][4:], "each permission that is off, one line"
    Client.meta = {**META, "screen_recording": True, "accessibility": True,
                   "event_synthesizing": True}
    assert await copies(services, p) == {"facts": [], "note": "Connected.", "badges": UPDATE}, \
        "all on: no grant"
    assert BLOCKED == {}, "all on: nothing blocked"

    # Full Disk Access and Local Network, as share.js says them: a protected folder waits
    # for one click on the Mac, so files and shell stay on
    gateway = sys.modules[copies.__module__]
    real = httpx.AsyncClient
    monkeypatch.setattr(gateway.httpx, "AsyncClient", lambda **kw: real(**kw, transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"full_disk_access": False, "local_network": False})
        if req.url.path == "/grants" and req.headers["authorization"] == "Bearer tok"
        else httpx.Response(404))))
    cfg.settings.plugins["mac"] = {"url": "http://mac:8765", "share": "http://mac:8766",
                                   "token": "tok"}
    got = await copies(services, p)
    assert got["lines"] == [
        {"text": "Full Disk Access is off: a folder like Desktop or Documents waits for a "
                 "click on the Mac's screen.", "tone": "warn"},
        {"text": "Local Network is off: sub-agents and shell commands on the Mac cannot reach the "
                 "app.", "tone": "error"}]
    assert got["note"] == ("Connected. On the Mac, open System Settings, Privacy & Security, "
                           "and turn on Celine under Full Disk Access and Local Network.")
    assert BLOCKED == {}, "no Mac server waits on a dialog for these"
    monkeypatch.setattr(gateway.httpx, "AsyncClient", real)
    cfg.settings.plugins["mac"] = {"url": "http://mac:8765"}
    BLOCKED.clear()

    # a screen server that does not answer holds the note for GRANTS_SECONDS at most
    class Silent:
        async def call_tool(self, name, args):
            await anyio.sleep_forever()
    monkeypatch.setattr(gateway, "GRANTS_SECONDS", 0.01)
    skills["macscreen"] = SimpleNamespace(status="up", error=None, client=Silent())
    assert await copies(services, p) == {"facts": [], "note": "Connected.", "badges": UPDATE}
    skills["macscreen"].client = Client()

    cfg.settings.plugins["mac"] = {}
    out = await p.attr("gateway:check")(services, p, None)
    assert out["lines"] == [{"text": gateway.NOT_YET, "tone": "warn"}], "one line before it connects"


async def test_a_look_at_the_screen_server_reads_the_grants_again_while_unknown_or_blocked(
        tmp_path, monkeypatch):
    """No timer, no form: a request's look at the screen server (Registry.down) reads the
    grants once after a start, then while they block it, at most every GRANTS_AGAIN."""
    from celine.skills import ASK, BLOCKED, Registry
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    gateway = sys.modules[plugins.get("mac").attr("gateway:copies").__module__]
    reads: list[str] = []

    class Client:
        meta = META

        async def call_tool(self, name, args):
            reads.append(name)
            return SimpleNamespace(meta=self.meta, content=[])
    services = SimpleNamespace(cfg=cfg, mcp=SimpleNamespace(skills={
        "macscreen": SimpleNamespace(status="up", error=None, client=Client())}))
    at = [100.0]
    grants = gateway.Grants(clock=lambda: at[0])
    ASK["macscreen"] = grants.ask
    look = Registry().down
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(grants.run, services)
            assert look("macscreen") is None and grants.due.is_set(), "unknown: a read"
            await until(lambda: "macscreen" in BLOCKED)
            assert look("macscreen").startswith("Screen Recording and Accessibility are off")
            assert not grants.due.is_set(), "at most every GRANTS_AGAIN"
            Client.meta = {**META, "screen_recording": True, "accessibility": True}
            at[0] += gateway.GRANTS_AGAIN
            assert look("macscreen") and grants.due.is_set(), "blocked: a read again"
            await until(lambda: "macscreen" not in BLOCKED)
            at[0] += gateway.GRANTS_AGAIN
            assert look("macscreen") is None and not grants.due.is_set(), "known and free"
            assert reads == ["permissions", "permissions"]
            tg.cancel_scope.cancel()
    finally:
        ASK.pop("macscreen", None)
        BLOCKED.pop("macscreen", None)


async def test_a_fresh_install_switches_its_mac_on_while_it_is_not_set_up(
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
        assert command["label"] == "Run in Terminal on the Mac"
        token = command["value"].split("/setup/")[1].split()[0]
        assert (await c.get(f"/api/plugins/mac/setup/{token}")).status_code == 200
        got = (await c.get("/api/features")).json()
        mac = next(f for f in got["catalog"] if f["id"] == "mac")
        assert got["features"]["mac"] is True
        assert (mac["state"], mac["blocks"]) == ("unset", False)
        assert mac["detail"] == "The Mac is not connected yet."
        tg.cancel_scope.cancel()


def test_the_mac_and_the_box_run_one_opencode():
    root = Path(__file__).parent.parent
    mac = re.search(r"^OPENCODE_VERSION=(\S+)", (root / "plugins/mac/setup.sh").read_text(), re.M)
    box = re.search(r"^ARG OPENCODE_VERSION=(\S+)", (root / "shellbox/Dockerfile").read_text(),
                    re.M)
    assert mac and box and mac[1] == box[1]


def test_setup_asks_for_local_network_and_opens_only_the_panes_that_are_off(tmp_path):
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node")
    text = (Path(__file__).parent.parent / "plugins/mac/setup.sh").read_text()
    assert "<key>NSLocalNetworkUsageDescription</key>" in text
    js = re.search(r"grants\(\) \{\n.*?\"\$NODE\" -e '\n(.*?)'\n\}", text, re.S)[1]

    def panes(**r):
        line = {"servers": {"screen": {"tools": 26, "missing": []}}, **r}
        out = subprocess.run([node, "-e", js], input=json.dumps(line), text=True,
                             capture_output=True, check=True).stdout
        return [x.split(" ", 2) for x in out.splitlines()]
    assert panes(full_disk_access=True, local_network=None) == [
        ["Privacy_ScreenCapture", "on", "Screen Recording"],
        ["Privacy_Accessibility", "on", "Accessibility"],
        ["Privacy_AllFiles", "on", "Full Disk Access"]], "an unknown grant has no pane"
    off = panes(servers={"screen": {"tools": 26, "missing": ["Event Synthesizing"]}},
                full_disk_access=False, local_network=False)
    assert [x[0] for x in off if x[1] == "off"] == [
        "Privacy_Accessibility", "Privacy_LocalNetwork", "Privacy_AllFiles"], "optional last"
    assert panes(servers={"screen": {"tools": None, "missing": []}}) == [], \
        "a screen server that does not answer says nothing of its grants"


def test_the_setup_version_covers_every_file_a_setup_gets(tmp_path, monkeypatch):
    scratch(tmp_path, monkeypatch)
    boot.load()
    gateway = sys.modules[plugins.get("mac").attr("gateway:update").__module__]
    served = tmp_path / "served"
    served.mkdir()
    for name in gateway.SERVED:
        shutil.copy(gateway.SCRIPT.with_name(name), served / name)
    monkeypatch.setattr(gateway, "SCRIPT", served / "setup.sh")
    seen = [gateway.version()]
    for name in gateway.SERVED:
        with (served / name).open("a") as f:
            f.write("\n")
        seen.append(gateway.version())
    assert len(set(seen)) == 4 and all(re.fullmatch(r"[0-9a-f]{12}", v) for v in seen)
    assert f"KEY=k SETUP={seen[-1]} JSON=''" in gateway.filled("http://192.168.1.20:8766", "k")


async def test_update_runs_the_apps_setup_on_the_mac_in_a_job_of_its_own(tmp_path, monkeypatch):
    import shlex
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    p = plugins.get("mac")
    update = p.attr("gateway:update")
    gateway = sys.modules[update.__module__]
    calls = []

    class Shell:
        said: str | None = None     # its answer; None: what the command prints once `open` ran

        async def call_tool(self, name, args):
            calls.append((name, args))
            text = self.said if self.said is not None else \
                f"Process started with PID 7\n{args['command'].rsplit(' ', 1)[1]}\n"
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                                   is_error=False)

    shell = SimpleNamespace(status="up", error=None, client=Shell())
    services = SimpleNamespace(cfg=cfg, mcp=SimpleNamespace(skills={"macshell": shell}))
    assert await update(services, p, None) == {"ok": True, "sentence": "The Mac is updating."}
    (name, args), = calls
    assert name == "start_process" and args["timeout_ms"] == 10000
    words = shlex.split(args["command"])
    # Celine.app opened anew: a launchd job of its own, outside the gateway's, under its grants
    assert words[:6] == ["open", "-ng", "$HOME/Applications/Celine.app", "--args", "/bin/bash",
                         "-c"]
    assert words[7:9] == ["&&", "echo"] and len(words) == 10
    m = re.fullmatch(r'exec >"\$HOME/Library/Logs/celine-update\.log" 2>&1; curl -fsSL (\S+)'
                     r" \| bash -s -- --json", words[6])
    assert m, words[6]
    url = shlex.split(m[1])[0]
    assert url.startswith(f"{gateway.address.base(cfg)}/api/plugins/mac/setup/")
    assert url.rsplit("/", 1)[1] in gateway._tokens, "a new setup line that works once"
    # pressed again while it runs: no second setup on the Mac
    assert await update(services, p, None) == {"ok": True, "sentence": "The Mac is updating."}
    assert len(calls) == 1
    gateway._updating.clear()                   # what the Mac's report does (POST /gateway)

    # `open` did not launch it: the shell's words
    shell.client.said = "Unable to find application named 'Celine'"
    assert await update(services, p, None) == {
        "ok": False, "sentence": "Shell: Unable to find application named 'Celine'"}
    # the shell server is down: Check's words, nothing runs
    calls.clear()
    services.mcp.skills["macshell"] = SimpleNamespace(status="down", client=None,
                                                      error="No route to host")
    assert await update(services, p, None) == {"ok": False, "sentence": "Shell: No route to host"}
    assert calls == []
