"""The agent's vault end to end with the fake bw (fake_bw.py): the Settings form and its status
line, its three tools, the redaction set, and a turn in which the password it reads
lands nowhere stored."""
from __future__ import annotations

import json

import pytest

from celine import box_files
from celine.backends.base import Done, TextDelta, ToolCallDelta
from celine.conversations import store as conversations
from celine.core.bus import EventBus
from celine.errors import redact
from celine.skills import Registry, ToolResult, tool
from celine.turns import store as turns
from celine.turns.cancel import Token
from celine.turns.runner import run_turn
from chat_fixtures import Script, until, user_says
from vault_fixtures import CLIENT_SECRET, FORM, MASTER, by_hand, ctx_for, fake_bw, vault_app  # noqa: F401

pytestmark = pytest.mark.anyio


def _secrets_in(calls: list[list[str]]) -> list[str]:
    return [a for c in calls for a in c if a in (CLIENT_SECRET, MASTER)]


async def test_the_form_saves_write_only_and_says_how_the_vault_is(vault_app):
    api, bw = vault_app, vault_app.bw
    got = await api.get("/api/vault")
    view = got.json()
    assert view["status"] == "Unlocked" and view["vault"]["client_id"] == FORM["vault"]["client_id"]
    assert view["client_secret"]["state"] == "set" and view["unlocked"] is True
    assert "master_password" not in view
    assert CLIENT_SECRET not in got.text and MASTER not in got.text
    assert bw.state()["url"] == "https://vault.example.com" and bw.state()["syncs"] == 1
    # unlocked, only the collection changes: it stays unlocked
    r = await api.put("/api/vault", json={"vault": {**FORM["vault"], "collection": "col-1"}})
    assert r.json()["unlocked"] is True and r.json()["status"] == "Unlocked"

    r = await api.put("/api/vault", json={**FORM, "client_secret": "a wrong secret"})
    assert r.json()["status"] == "The vault refuses the client id or the client secret"
    bw.change(reachable=False)
    r = await api.put("/api/vault", json=FORM)
    assert r.json()["status"] == "The vault server is not reachable"
    bw.change(reachable=True)
    # a secret left out keeps the saved one; a save logs in again, locked
    r = await api.put("/api/vault", json={"vault": FORM["vault"]})
    assert r.json()["status"].startswith("Locked") and r.json()["unlocked"] is False
    r = await api.put("/api/vault", json={"vault": FORM["vault"], "client_secret": None})
    assert r.json()["status"] == "Not set up" and r.json()["client_secret"]["state"] == "not set"
    assert api.skill.status == "down"
    assert _secrets_in(bw.calls()) == [], "a secret goes to bw by environment, never as an argument"
    bad = await api.put("/api/vault", json={"vault": {"url": "vault.example.com"}})
    assert bad.status_code == 400 and bad.json()["error"]["detail"]["field"] == "vault.url"


async def test_the_status_says_when_the_vault_is_locked(vault_app):
    api = vault_app
    assert (await api.get("/api/status")).json()["vault_locked"] is False
    await api.post("/api/vault/lock")
    assert (await api.get("/api/status")).json()["vault_locked"] is True


async def test_the_master_password_is_typed_to_unlock_and_kept_nowhere(vault_app, store):
    """Unlock takes it once; nothing on disk holds it; lock and a new login forget the
    session; a wrong one says so; it gets "locked" until the user types it again."""
    api, bw = vault_app, vault_app.bw
    data = vault_app.cfg.data_dir
    assert not [p for p in data.rglob("*") if p.is_file() and MASTER.encode() in p.read_bytes()]
    r = await api.post("/api/vault/lock")
    assert r.json()["unlocked"] is False and r.json()["status"].startswith("Locked")
    out = await vault_app.skill.get(ctx_for(store))
    assert out.is_error and "the vault is locked" in out.content
    r = await api.post("/api/vault/unlock", json={"master_password": "not it"})
    assert r.json()["status"] == "The vault refuses the master password"
    r = await api.post("/api/vault/unlock", json={"master_password": MASTER})
    assert r.json()["status"] == "Unlocked"
    await vault_app.vault.connect()            # what a restart or a save does
    assert vault_app.vault.session is None and vault_app.vault.status.startswith("Locked")
    assert _secrets_in(bw.calls()) == []


async def test_only_a_logged_in_person_unlocks_and_answers(vault_app):
    """With passkeys on, a named token (its shellbox, a script) gets 403 on every vault
    write."""
    from types import SimpleNamespace
    vault_app.app.state.auth = SimpleNamespace()        # passkeys on, no session here
    for path, body in (("/api/vault/unlock", {"master_password": MASTER}),
                       ("/api/vault/lock", None),
                       ):
        r = await vault_app.post(path, json=body)
        assert r.status_code == 403 and r.json()["error"]["code"] == "passkey_only", path
    r = await vault_app.put("/api/vault", json=FORM)
    assert r.status_code == 403
    vault_app.app.state.auth = None


async def test_set_get_and_rotate_and_she_never_sees_a_password(vault_app, store, monkeypatch):
    skill, bw = vault_app.skill, vault_app.bw
    ctx = ctx_for(store)
    out = await skill.set(ctx, name="example-shop", username="celine@example.com",
                          url="https://example-shop.com")
    [item] = bw.state()["items"]
    first = item["login"]["password"]
    assert len(first) == 24 and first not in out.content
    assert out.content.splitlines() == [
        "Made example-shop: celine@example.com, https://example-shop.com",
        'Into a password field: {"do": "type", "target": "<field id>", "text": '
        '"vault:example-shop/password"}']
    assert redact(f"x {first} y") == "x **** y", "a new password is in the redaction set"
    again = await skill.set(ctx, name="example-shop")
    assert again.is_error and again.content == "example-shop exists: vault__rotate(name) changes it."
    assert (await skill.set(ctx, name="Anocris Game")).is_error

    written = []

    async def put(cfg, src, rel, **kw):
        written.append((src, rel))
        return {"path": f"/home/celine/{rel}", "bytes": len(src)}
    monkeypatch.setattr(box_files, "put", put)
    got = await skill.get(ctx, name="example-shop", shell=True, why="the order script logs in")
    assert written == [(first.encode(), ".vault/example-shop/password")]
    assert first not in got.content and got.content.endswith("For a script: ~/.vault/example-shop/password")

    out = await skill.rotate(ctx, name="example-shop", why="it was sent to the model")
    [item] = bw.state()["items"]
    new = item["login"]["password"]
    assert new != first and item["passwordHistory"][0]["password"] == first
    assert new not in out.content and '"text": "vault:example-shop/previous"' in out.content
    assert redact(new) == "****"
    it, old = await vault_app.vault.secret("example-shop", "previous")
    assert (it["name"], old) == ("example-shop", first)
    assert [a for a, _ in vault_app.taps] == [          # a new entry does not ask
        "put the password of example-shop into a file in the shellbox",
        "change the password of example-shop"]
    listing = await skill.get(ctx)
    assert listing.content == "example-shop: celine@example.com, https://example-shop.com"
    assert (await skill.get(ctx, name="nope")).content == \
        "There is no vault entry nope. vault__set(name) makes one."
    assert _secrets_in(bw.calls()) == [] and not [c for c in bw.calls() if first in c or new in c]


async def test_any_name_an_entry_has_is_taken_and_one_that_differs_by_case_is_named(
        vault_app, store, monkeypatch):
    """get and rotate take every name an entry has: capitals, spaces, a slash; the exact
    name wins, else the one that differs only by case, and the result says which. set
    keeps making simple lowercase names. A name that cannot be a folder gets no file."""
    skill, ctx = vault_app.skill, ctx_for(store)
    vault_app.bw.change(items=by_hand("GitHub", "Github", "My Bank", "work/mail", "../up"))
    written = []

    async def put(cfg, src, rel, **kw):
        written.append(rel)
        return {"path": f"/home/celine/{rel}", "bytes": len(src)}
    monkeypatch.setattr(box_files, "put", put)

    got = (await skill.get(ctx, name="github")).content.splitlines()
    assert got[:3] == ["GitHub: user0, https://bank.example", "This is GitHub; Github differs from it only by case.",
                       'Into a password field: {"do": "type", "target": "<field id>", '
                       '"text": "vault:GitHub/password"}']
    assert (await skill.get(ctx, name="GITHUB")).content.startswith("GitHub: user0, https://bank.example\n")
    got = await skill.get(ctx, name="my bank", shell=True)
    assert got.content.splitlines()[0] == "My Bank: user2, https://bank.example"
    got = await skill.get(ctx, name="work/mail", shell=True)
    assert got.content.endswith("For a script: ~/.vault/work/mail/password")
    got = await skill.get(ctx, name="../up", shell=True)
    assert got.is_error and got.content == (
        '../up cannot be a folder in ~/.vault; the file was not written. A form takes it as '
        '{"do": "type", "target": "<field id>", "text": "vault:../up/password"}')
    assert written == [".vault/My Bank/password", ".vault/work/mail/password"]

    assert (await skill.set(ctx, name="GitHub")).content.startswith("A new vault name is lowercase")
    assert (await skill.set(ctx, name="github")).content == \
        "GitHub exists: vault__rotate(name) changes it."
    out = await skill.rotate(ctx, name="my bank")
    assert out.content.splitlines()[0] == "My Bank has a new password."
    assert 'vault__get(name="My Bank", shell=true)' in out.content
    bank = next(it for it in vault_app.bw.state()["items"] if it["name"] == "My Bank")
    assert bank["passwordHistory"][0]["password"] == "test-hand-made-0002"
    it, old = await vault_app.vault.secret("MY BANK", "previous")
    assert (it["name"], old) == ("My Bank", "test-hand-made-0002")


async def test_a_bw_that_keeps_no_history_gets_the_old_one_by_hand(vault_app, store):
    vault_app.bw.change(history=False)
    ctx = ctx_for(store)
    await vault_app.skill.set(ctx, name="example-shop")
    first = vault_app.bw.state()["items"][0]["login"]["password"]
    await vault_app.skill.rotate(ctx, name="example-shop")
    [item] = vault_app.bw.state()["items"]
    assert [h["password"] for h in item["passwordHistory"]] == [first]


async def test_a_lost_session_locks_the_vault_until_the_user_unlocks(vault_app, store):
    """Nothing unlocks on its own: a session bw no longer takes means locked, and it is
    told; the user's unlock opens it again."""
    bw, ctx = vault_app.bw, ctx_for(store)
    bw.change(session="session-2")              # the session expired on the server's side
    out = await vault_app.skill.get(ctx)
    assert out.is_error and out.content == ("Nothing was done: the vault is locked. Tell "
                                            f"{vault_app.cfg.user}.")
    assert vault_app.vault.status.startswith("Locked")
    r = await vault_app.post("/api/vault/unlock", json={"master_password": MASTER})
    assert r.json()["status"] == "Unlocked"
    assert (await vault_app.skill.get(ctx)).content.startswith("The vault is empty")


async def test_a_bw_answer_with_no_sentence_of_its_own_is_said_plainly_and_logged(
        vault_app, store, caplog):
    """What bw said reaches the log only; it and the status line get a plain sentence,
    and the next command that works clears it."""
    vault_app.bw.change(odd="Error: Weird server reply 0x1f, see the bw docs")
    out = await vault_app.skill.set(ctx_for(store), name="example-shop")
    assert out.is_error and out.content == ("Nothing was done: the vault gave an error. Tell "
                                            f"{vault_app.cfg.user}.")
    assert vault_app.vault.status == "The vault gave an error"
    [rec] = [r for r in caplog.records if getattr(r, "event", "") == "vault.bw_failed"]
    assert (rec.command, rec.said) == ("create item",
                                       "Error: Weird server reply 0x1f, see the bw docs")
    assert (await vault_app.get("/api/vault")).json()["status"] == \
        "The vault gave an error"
    await vault_app.skill.get(ctx_for(store))
    assert vault_app.vault.status == "Unlocked"


async def test_what_she_reads_from_the_file_is_stored_nowhere(vault_app, store):
    """`cat ~/.vault/example-shop/password` in its shell, then the value in its arguments and in
    its reply, streamed in two pieces: no stored message, tool row, raw answer or event
    holds it. A token the user handed it is not a known secret: it reaches the tool."""
    await vault_app.skill.set(ctx_for(store), name="example-shop")
    pw = vault_app.bw.state()["items"][0]["login"]["password"]

    class T:                      # its shell, as far as this turn goes
        name, description = "t", "test"

        @tool(read_only=False)
        async def cat(self, ctx, path: str) -> ToolResult:
            """cat"""
            return ToolResult(content=pw, display={"output": f"$ cat {path}\n{pw}"})

        @tool(read_only=False)
        async def say(self, ctx, text: str) -> ToolResult:
            """say"""
            heard.append(text)
            return ToolResult(content=f"said {text}")

    heard = []
    token = "Authorization: Bearer abcdefghijklmnop1234"

    reg = Registry(app=vault_app.app.state.services)
    reg.add(T())
    backend = Script([
        [ToolCallDelta(index=0, id="r1", name="t__cat",
                       arguments=json.dumps({"path": "~/.vault/example-shop/password"})),
         Done(reason="tool_calls")],
        [ToolCallDelta(index=0, id="r2", name="t__say",
                       arguments=json.dumps({"text": f"{pw} {token}"})),
         Done(reason="tool_calls")],
        [TextDelta(f"It is {pw[:12]}"), TextDelta(f"{pw[12:]}, friend."),
         Done(reason="stop", raw={"text": f"It is {pw}"})]])
    cid = await conversations.create(store)
    await user_says(store, cid, "what is the example-shop password?")
    turn = await turns.claim(store, "w")
    await run_turn(turn, store, vault_app.cfg, backend, EventBus(), Token(turn["id"]), reg)

    rows = await store.read(lambda c: [
        *(r[0] for r in c.execute("SELECT content FROM messages")),
        *(f"{r[0]} {r[1]} {r[2]}" for r in c.execute(
            "SELECT arguments, display, withheld_output FROM tool_calls")),
        *(r[0] for r in c.execute("SELECT body FROM raw_responses")),
        *(r[0] for r in c.execute("SELECT data FROM events"))])
    assert "It is ****, friend." in rows
    assert not [r for r in rows if r and (pw[:12] in r or pw[12:] in r)]
    assert heard == [f"**** {token}"]
    assert "****" in backend.calls[1][-1]["content"], "it reads the result redacted too"


async def test_no_turn_starts_before_the_first_login_is_over(vault_app, store):
    """The app logs in at start in the background; its turns wait for it."""
    import anyio
    from fake_backend import FakeBackend
    from celine.conversations import view
    from celine.turns.dispatcher import Dispatcher
    from celine.vault import Vault
    vault = Vault(vault_app.cfg)
    d = Dispatcher(store, vault_app.cfg, EventBus(), FakeBackend, Registry())
    d.ready = vault.ready
    cid = await conversations.create(store)
    await user_says(store, cid, "hi")

    async def state():
        return (await view.snapshot(store, cid))["turn"]["state"]
    async with anyio.create_task_group() as tg:
        await d.start(tg)
        d.notify()
        await anyio.wait_all_tasks_blocked()     # the dispatcher waits for the login
        assert await state() == "queued"
        assert (await vault.connect()).startswith("Locked")

        async def done():
            return await state() == "done"
        await until(done, 8)
        await d.stop()
        tg.cancel_scope.cancel()


async def test_with_a_shared_collection_her_entries_go_there_and_she_sees_only_it(vault_app, store):
    """Settings, Vault, collection: a new entry gets the collection and its organization,
    so the user sees it; its own vault and other collections are out of its sight. A
    collection it is not in says so and makes nothing."""
    api, bw, ctx = vault_app, vault_app.bw, ctx_for(store)
    bw.change(collections=[{"id": "col-1", "organizationId": "org-1", "name": "celine"},
                           {"id": "col-2", "organizationId": "org-1", "name": "dupe"},
                           {"id": "col-3", "organizationId": "org-1", "name": "dupe"}],
              items=[{"type": 1, "id": "mine", "name": "personal", "collectionIds": [],
                      "organizationId": None, "passwordHistory": None,
                      "login": {"username": "x", "password": "test-personal-0001",
                                "uris": [{"uri": "https://p.example"}]}}])
    r = await api.put("/api/vault", json={"vault": {**FORM["vault"], "collection": "Celine"}})
    assert r.status_code == 200
    await api.post("/api/vault/unlock", json={"master_password": MASTER})
    assert (await vault_app.skill.get(ctx)).content.startswith("The vault is empty")
    out = await vault_app.skill.set(ctx, name="reddit", url="https://www.reddit.com")
    assert not out.is_error, out.content
    made = next(i for i in bw.state()["items"] if i["name"] == "reddit")
    assert made["organizationId"] == "org-1" and made["collectionIds"] == ["col-1"]
    names = (await vault_app.skill.get(ctx)).content
    assert "reddit" in names and "personal" not in names
    # a collection it is not in, or a name two share: the unlock says so, in Settings
    await api.put("/api/vault", json={"vault": {**FORM["vault"], "collection": "nope"}})
    r = await api.post("/api/vault/unlock", json={"master_password": MASTER})
    assert "not one the agent's account is in" in r.json()["status"] and r.json()["unlocked"] is False
    out = await vault_app.skill.set(ctx, name="lemmy", url="https://lemmy.example")
    assert out.is_error
    assert not [i for i in bw.state()["items"] if i["name"] == "lemmy"]
    await api.put("/api/vault", json={"vault": {**FORM["vault"], "collection": "dupe"}})
    r = await api.post("/api/vault/unlock", json={"master_password": MASTER})
    assert "give its id instead" in r.json()["status"]
    await api.put("/api/vault", json={"vault": {**FORM["vault"], "collection": "col-1"}})
    await api.post("/api/vault/unlock", json={"master_password": MASTER})
    assert "reddit" in (await vault_app.skill.get(ctx)).content       # by id works too
    # no collection: no personal vault to fall back to, the vault takes nothing
    await api.put("/api/vault", json={"vault": {**FORM["vault"], "collection": ""}})
    r = await api.post("/api/vault/unlock", json={"master_password": MASTER})
    assert "No collection" in r.json()["status"] and r.json()["unlocked"] is False
    out = await vault_app.skill.set(ctx, name="hidden", url="https://h.example")
    assert out.is_error
    assert not [i for i in bw.state()["items"] if i["name"] == "hidden"]


async def test_the_first_unlock_syncs_even_right_after_the_host_booted(vault_app, monkeypatch):
    import celine.vault as vault_mod
    monkeypatch.setattr(vault_mod.time, "monotonic", lambda: 10.0)    # up 10 s
    before = vault_app.bw.state()["syncs"]
    await vault_app.post("/api/vault/lock")
    await vault_app.post("/api/vault/unlock", json={"master_password": MASTER})
    assert vault_app.bw.state()["syncs"] == before + 1
