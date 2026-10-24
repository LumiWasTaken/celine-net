"""Triggers: declared by skills, off until the user turns them on, a fire is a background
task in its own conversation hidden from the chat list, the mail poller, start_chat."""
from __future__ import annotations

import tomllib
from types import SimpleNamespace

import anyio
import pytest

from celine import boot
from celine.conversations import store as conversations, view
from celine.intercom import Intercom
from celine.triggers import Event, Triggers, Watch, declared
from celine.turns import store as turns
from chat_fixtures import until
from settings_fixtures import client, make_app, scratch

pytestmark = pytest.mark.anyio


@pytest.fixture
async def api(tmp_path, monkeypatch, store):
    from celine.triggers import api as triggers_api
    var = scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    app = make_app(cfg, store)
    app.include_router(triggers_api.router)
    async with client(app) as c:
        c.var, c.cfg = var, cfg
        yield c


async def test_the_mail_trigger_is_declared_off_and_the_user_turns_it_on(api):
    got = (await api.get("/api/triggers")).json()["triggers"]
    assert {"name": "mail.new", "label": "New email", "about": "Reads new mail and "
            "asks you only when something needs you.", "skill": "email", "kind": "event",
            "on": False, "fired_at": None} in got
    r = await api.put("/api/triggers/mail.new", json={"on": True})
    assert r.status_code == 200 and r.json()["on"] is True
    saved = tomllib.loads((api.var / "settings" / "triggers.toml").read_text())
    assert saved == {"mail.new": {"on": True}}
    assert (await api.put("/api/triggers/mail.new", json={"on": True, "cron": "0 7 * * *"})
            ).status_code == 400, "a trigger has a switch only"
    assert (await api.put("/api/triggers/nope", json={"on": True})).status_code == 404


def _app(store, cfg, registry=None):
    return SimpleNamespace(cfg=cfg, store=store, intercom=Intercom(store), registry=registry)


async def test_a_fire_is_a_background_task_hidden_from_the_chat_list(store, api):
    t = Triggers(_app(store, boot.load()))
    cid = await t.fire("email", "mail.new", "New mail.")
    assert await t.fire("email", "mail.new", "More mail.") == cid
    turn = await turns.claim(store, "w")
    await turns.transition(store, turn["id"], cid, "done")
    await t.app.intercom.deliver_idle(cid)
    users = [m for m in (await view.snapshot(store, cid))["messages"] if m["role"] == "user"]
    assert [(m["intercom"]["kind"], m["intercom"]["from"]) for m in users] == [
        ("trigger", "system:email")] * 2
    assert users[0]["content"].startswith("New mail.\n\nThis is a background task")
    assert cid not in [c["id"] for c in (await conversations.list(store))]
    [row] = (await api.get("/api/background-conversations")).json()["background"]
    assert row["id"] == cid and row["kind"] == "background" and row["trigger"] == "mail.new"


async def test_a_watch_fires_once_per_batch_with_its_paths(store, tmp_path, monkeypatch):
    import watchfiles
    skill = SimpleNamespace(name="files", triggers=(Watch(
        "inbox", str(tmp_path), ("added",), "{path} ({event}) appeared.", debounce=0.2,
        label="Inbox", about="New files."),))
    registry = SimpleNamespace(skills={"files": skill})
    cfg = boot.load()
    cfg.settings.triggers = {"inbox": {"on": True}}
    assert set(declared(registry)) == {"inbox"}
    a, b = str(tmp_path / "a.txt"), str(tmp_path / "b.txt")
    watched = anyio.Event()

    async def awatch(path, **_):          # one debounced batch, then the folder is quiet
        assert str(path) == str(tmp_path)
        yield {(watchfiles.Change.added, a), (watchfiles.Change.added, b),
               (watchfiles.Change.modified, a)}
        watched.set()
    monkeypatch.setattr(watchfiles, "awatch", awatch)

    async def users():
        rows = await store.read(lambda c: c.execute(
            "SELECT id FROM conversations WHERE kind='background'").fetchall())
        snap = await view.snapshot(store, rows[0]["id"]) if rows else {"messages": []}
        return [m["content"] for m in snap["messages"] if m["role"] == "user"]

    async with anyio.create_task_group() as tg:
        await Triggers(_app(store, cfg, registry)).run(tg)
        await watched.wait()
        tg.cancel_scope.cancel()
    [prompt] = await users()
    assert prompt.startswith(f"{a}\n{b} (added) appeared.")


async def test_a_failed_fire_is_logged_and_the_trigger_keeps_asking(store):
    asked = []

    class Skill:
        name = "mail"
        triggers = (Event("ping", "source", "{x}", every=0.01, label="Ping", about="."),)

        async def source(self, _store):
            asked.append(1)
            return [{"x": "y"}]

    cfg = boot.load()
    cfg.settings.triggers = {"ping": {"on": True}}
    t = Triggers(_app(store, cfg, SimpleNamespace(skills={"mail": Skill()})))

    async def broken(*a):
        raise RuntimeError("database is locked")
    t.fire = broken
    async with anyio.create_task_group() as tg:
        await t.run(tg)
        await until(lambda: len(asked) >= 3, 2)
        tg.cancel_scope.cancel()


class _Imap:
    """Just enough of imaplib for new_mail: INBOX with uids 1..n, the last two unread."""
    def __init__(self, n):
        self.n = n

    def select(self, *a, **k):
        return "OK", [b""]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            if args == ("ALL",):
                return "OK", [" ".join(map(str, range(1, self.n + 1))).encode()]
            lo = int(args[1].split(":")[0])
            hits = [u for u in range(self.n - 1, self.n + 1) if u >= lo] or [self.n]
            return "OK", [" ".join(map(str, hits)).encode()]
        out = []
        for u in args[0].split(","):
            out.append((f"{u} (UID {u} FLAGS ())".encode(),
                        f"From: a@b.c\r\nSubject: hello {u}\r\n\r\n".encode()))
            out.append(b")")
        return "OK", out


async def test_new_mail_asks_once_to_learn_the_inbox_then_gives_each_new_message(
        store, monkeypatch):
    from celine.skills import mail
    box = _Imap(5)
    monkeypatch.setattr(mail, "account", lambda cfg: object())
    monkeypatch.setattr(mail, "session", lambda a, fn, timeout=30: fn(box))
    skill = mail.Email(boot.load())
    assert await skill.new_mail(store) == [], "the first ask only notes the newest uid"
    assert await skill.new_mail(store) == []
    box.n = 7
    got = await skill.new_mail(store)
    assert [(m["id"], m["subject"]) for m in got] == [("INBOX:6", "hello 6"),
                                                       ("INBOX:7", "hello 7")]
    assert await skill.new_mail(store) == [], "n:* matching the newest is not new"


async def test_start_chat_is_only_offered_away_from_the_user(
        store):
    from celine.intercom.skill import IntercomSkill
    cfg = boot.load()
    skill = IntercomSkill(cfg)
    skill.app = SimpleNamespace(intercom=Intercom(store))
    chat = await conversations.create(store, "talk")
    bg = await conversations.for_trigger(store, "trigger:mail.new", "mail.new")
    assert await skill.offered_in(store, chat) == {"send", "read", "cancel"}
    assert await skill.offered_in(store, bg) is True
