"""She configures herself (skills/settings.py): settings__read shows the settings with no
secret in them; settings__propose checks at once, asks the user (approvals, kind
`settings`), and once approved saves through the Settings save (celine/saves.py) on
the version it was proposed against; a secret goes from a file in her box into
var/secrets and nowhere else."""
from __future__ import annotations

import json
import logging
import tomllib

import anyio
import httpx
import pytest
from chat_fixtures import until
from settings_fixtures import KEY, scratch

from celine import approvals as ap, boot
from celine.conversations import store as conversations
from celine.core import ids
from celine.core.bus import EventBus
from celine.core.services import Services
from celine.guard.owner import _SEAL, Owner
from celine import saves
from celine.settings import secrets as secrets_mod
from celine.settings import her_models
from celine.settings.files import version
from celine.skills import ToolContext, build
from celine.skills.settings import Settings

pytestmark = pytest.mark.anyio
OWNER = Owner("test", _SEAL)
PASSWORD = "imap-pass-7f3e9a1c55"
T0 = "2026-10-29T09:00:00.000+00:00"


class Box:
    """A shellbox that holds one file, ~/pw.txt."""

    def __init__(self, text: str):
        self.text = text
        self.reads = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.reads += 1
        if request.url.path == "/file" and request.url.params["path"] == "~/pw.txt":
            return httpx.Response(200, content=self.text.encode())
        return httpx.Response(404, json={"error": "No file there."})


@pytest.fixture
async def app(tmp_path, monkeypatch, store):
    monkeypatch.setattr(ids, "now", lambda: T0)
    monkeypatch.setenv("CELINE_SHELL_URL", "http://shellbox:8770")
    var = scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    cfg.user = "sam"
    services = Services(cfg=cfg, store=store, bus=EventBus())
    services.registry = build(services)
    services.approvals = ap.Approvals(cfg, store=store)
    services.feature_probes = (0, {})       # no probe: the saved features are what counts
    services.var = var
    services.seen = []
    store.emit_global = services.seen.append
    return services


def _skill(app, box: Box | None = None) -> Settings:
    s = Settings(app.cfg, transport=httpx.MockTransport(box.handle) if box else None)
    s.app = app
    return s


async def _ctx(app) -> ToolContext:
    cid = await conversations.create(app.store)
    return ToolContext(conversation_id=cid, turn_id="t1", tool_call_id="k1", store=app.store,
                       config=app.cfg, emit=None)  # type: ignore[arg-type]


async def _answer(app, call, ok: bool, meanwhile=None):
    """Run `call`, answer its ask with `ok` (after `meanwhile()`) -> (result, the ask)."""
    out: dict = {}

    async def run():
        out["result"] = await call

    async with anyio.create_task_group() as tg:
        tg.start_soon(run)
        await until(lambda: app.approvals.waiting)
        ask = next(iter(app.approvals.waiting.values()))
        if meanwhile is not None:
            await meanwhile()
        assert app.approvals.decide(ask.id, ok, OWNER)
    return out["result"], ask


async def test_the_read_never_holds_a_secret(app):
    root = secrets_mod.secrets_dir(app.cfg.data_dir)
    secrets_mod.set_key(root, "or", KEY)
    secrets_mod.set_key(root, "email", PASSWORD)
    sk = _skill(app)
    ctx = await _ctx(app)
    index = json.loads((await sk.read(ctx)).content)["targets"]
    ids_ = [t["target"] for t in index]
    assert {"features", "general", "roles", "tools", "context", "email",
            "providers.or"} <= set(ids_)
    texts = [(await sk.read(ctx, target=t)).content for t in ids_]
    assert all(KEY not in t and PASSWORD not in t for t in texts)
    email = json.loads(texts[ids_.index("email")])
    assert {"key": "password", "label": "Password", "type": "secret", "state": "set"} in \
        email["fields"]
    provider = json.loads(texts[ids_.index("providers.or")])
    assert next(f for f in provider["fields"] if f["key"] == "key")["state"] == "set"
    assert email["version"] == version(app.cfg.settings.email)


async def test_an_invalid_proposal_fails_at_once_and_asks_nothing(app):
    sk = _skill(app)
    ctx = await _ctx(app)
    got = await sk.propose(ctx, target="general",
                           changes={"turn_wall_clock_seconds": "soon"}, why="x")
    assert got.is_error
    assert got.content == "general.turn_wall_clock_seconds: This must be a whole number."
    got = await sk.propose(ctx, target="general",
                           changes={"job_timeout_seconds": 10}, why="x")
    assert got.is_error and "longer than the tool time limit" in got.content
    got = await sk.propose(ctx, target="roles", changes={"chat": ["nope/x"]}, why="x")
    assert got.is_error and "no provider 'nope'" in got.content
    got = await sk.propose(ctx, target="context", changes={"memory_sections": 99}, why="x")
    assert got.content == "context.memory_sections: memory_sections is a whole number " \
                          "from 0 to 20, such as 3."
    got = await sk.propose(ctx, target="general", changes={"nope": 1}, why="x")
    assert got.is_error and got.content.startswith("Not a setting of general: nope.")
    assert not app.approvals.waiting and not await _approval_events(app)


async def _approval_events(app):
    return await app.store.read(lambda c: [(r[0], json.loads(r[1])) for r in c.execute(
        "SELECT type, data FROM events WHERE type LIKE 'approval.%' ORDER BY seq")])


async def test_a_valid_proposal_asks_with_before_and_after_and_deny_changes_nothing(app):
    sk = _skill(app)
    ctx = await _ctx(app)
    before = (app.var / "settings" / "general.toml").read_bytes()
    got, ask = await _answer(app, sk.propose(
        ctx, target="general", changes={"turn_wall_clock_seconds": 3600},
        why="Long research turns stop at the limit."), False)
    assert ask.kind == "settings" and ask.call == "k1"
    (kind, asked), (closed, _) = await _approval_events(app)
    assert (kind, closed) == ("approval.asked", "approval.closed")
    chat = asked["approval"]
    assert chat["label"] == "Settings" and chat["line"] == "Celine asks to change the General settings."
    assert chat["changes"] == [{"label": "Turn time limit", "before": "1800",
                                "after": "3600"}]
    assert chat["why"] == "Long research turns stop at the limit."
    assert got.is_error and got.content == "sam denied it; nothing was done."
    assert (app.var / "settings" / "general.toml").read_bytes() == before
    assert "turn_wall_clock_seconds" not in app.cfg.settings.general


async def test_approve_saves_through_settings_and_bumps_the_version(app):
    sk = _skill(app)
    ctx = await _ctx(app)
    v0 = version(app.cfg.settings.general)
    got, _ = await _answer(app, sk.propose(
        ctx, target="general", changes={"turn_wall_clock_seconds": 3600}, why="w"), True)
    assert not got.is_error, got.content
    assert got.content == "Changed General. Turn time limit: 1800 → 3600."
    assert app.cfg.settings.general["turn_wall_clock_seconds"] == 3600
    assert version(app.cfg.settings.general) != v0
    on_disk = tomllib.loads((app.var / "settings" / "general.toml").read_text())
    assert on_disk["turn_wall_clock_seconds"] == 3600
    assert {"type": "settings.changed", "what": ["general"]} in app.seen


async def test_a_feature_switched_on_says_what_its_probe_finds(app, monkeypatch):
    from celine.skills import web

    async def down(cfg, services):
        return {"state": "missing", "detail": "page words", "why": "SearXNG is not running."}
    monkeypatch.setattr(web, "probe", down)
    await saves.features(app, {"web": False})
    sk = _skill(app)
    got, _ = await _answer(app, sk.propose(
        await _ctx(app), target="features", changes={"web": True}, why="w"), True)
    assert got.content == ("Changed Features. Web search: Off → On. "
                           "Web search: missing. SearXNG is not running."), got.content


async def test_an_approve_after_the_setting_changed_fails_and_keeps_the_change(app):
    sk = _skill(app)
    ctx = await _ctx(app)

    async def meanwhile():      # the user saves General in Settings while it waits
        await saves.general(app, {**app.cfg.settings.general,
                                  "turn_wall_clock_seconds": 900})
    got, _ = await _answer(app, sk.propose(
        ctx, target="general", changes={"turn_wall_clock_seconds": 3600}, why="w"), True,
        meanwhile)
    assert got.is_error
    assert got.content == ("General changed since you proposed it; nothing was done. "
                           "Read it again.")
    assert app.cfg.settings.general["turn_wall_clock_seconds"] == 900


async def test_a_secret_goes_from_her_box_to_var_secrets_and_nowhere_else(app, caplog):
    caplog.set_level(logging.DEBUG)
    box = Box(PASSWORD + "\n")
    sk = _skill(app, box)
    ctx = await _ctx(app)
    plain = await sk.propose(ctx, target="email", changes={"password": "typed"}, why="w")
    assert plain.is_error and '{"file": "~/path"}' in plain.content
    gone, _ = await _answer(app, sk.propose(
        ctx, target="email", changes={"host": "imap.example.com",
                                      "password": {"file": "~/no"}}, why="w"), True)
    assert gone.content == "email.password: No file there. Nothing was done."
    assert "host" not in app.cfg.settings.email, "a secret not read saves nothing"
    big, _ = await _answer(app, _skill(app, Box("x" * 70_000)).propose(
        ctx, target="email", changes={"password": {"file": "~/pw.txt"}}, why="w"), True)
    assert big.content == ("email.password: ~/pw.txt is larger than a secret. Nothing "
                           "was done.")
    box.reads = 0

    async def unread():         # the file is read once approved, not before
        assert box.reads == 0
    got, ask = await _answer(app, sk.propose(
        ctx, target="email", changes={"host": "imap.example.com",
                                      "password": {"file": "~/pw.txt"}},
        why="The mail skill needs the account."), True, unread)
    assert not got.is_error, got.content
    assert box.reads == 1
    assert ask.changes == [{"label": "IMAP server", "before": "Empty",
                            "after": "imap.example.com"},
                           {"label": "Password", "after": "New value"}]
    root = secrets_mod.secrets_dir(app.cfg.data_dir)
    assert secrets_mod.read_key(root, "email") == PASSWORD
    assert app.cfg.settings.email["host"] == "imap.example.com"
    events = await app.store.read(lambda c: [r[0] for r in c.execute(
        "SELECT data FROM events")])
    read = (await sk.read(ctx, target="email")).content
    for text in (got.content, json.dumps(got.display), json.dumps(ask.view()),
                 json.dumps(ap.chat(ask, None)), *events, caplog.text, read,
                 json.dumps(app.seen)):
        assert PASSWORD not in text
    assert '"state": "set"' in read


async def test_every_target_saves_through_its_settings_save(app):
    class Yes:
        async def ask(self, *a, **k):
            return None
    app.approvals = Yes()
    sk = _skill(app)
    ctx = await _ctx(app)
    for target, changes, check in (
        ("features", {"web": False}, lambda s: s.features["web"] is False),
        ("roles", {"chat": ["main/big", "lan/small"]},
         lambda s: s.roles["chat"]["chain"] == ["main/big", "lan/small"]),
        ("tools", {"mind": "off"}, lambda s: s.tools == {"mind": "off"}),
        ("providers.lan", {"label": "The GPU box"},
         lambda s: s.providers["lan"]["label"] == "The GPU box"),
        ("services", {"shellbox": "http://box:8770"},
         lambda s: s.services["shellbox"]["url"] == "http://box:8770"),
    ):
        got = await sk.propose(ctx, target=target, changes=changes, why="w")
        assert not got.is_error, (target, got.content)
        assert check(app.cfg.settings), target
    got = await sk.propose(ctx, target="context", changes={"memory_sections": 9}, why="w")
    assert not got.is_error, got.content
    assert app.cfg.context["memory_sections"] == 9
    kept = tomllib.loads((app.var / "settings" / "context.toml").read_text())
    assert kept == {"memory_sections": 9}, "only what differs from the defaults"
    again = await sk.propose(ctx, target="context", changes={"memory_sections": 9}, why="w")
    assert again.content == "Nothing changes: these are the values already."


async def test_a_call_bastet_let_through_still_shows_its_settings_card(app):
    sk = _skill(app)
    ctx = await _ctx(app)
    app.approvals.approved["k1"] = None         # approved on Bastet's ask
    got, ask = await _answer(app, sk.propose(
        ctx, target="general", changes={"turn_wall_clock_seconds": 3600}, why="w"), True)
    assert ask.changes and not got.is_error


async def test_a_refused_reload_is_reported(app, monkeypatch):
    from celine import reload
    from celine.config import Problem

    def refused(services, **_):
        services.cfg.problems = [Problem("config", "llm.toml does not parse", fatal=True)]
        return False
    monkeypatch.setattr(reload, "apply", refused)

    class Yes:
        async def ask(self, *a, **k):
            return None
    app.approvals = Yes()
    got = await _skill(app).propose(await _ctx(app), target="context",
                                    changes={"memory_sections": 9}, why="w")
    assert got.is_error and got.content == (
        "Saved Context (memory_sections: 3 \u2192 9), but the app refused to load it and keeps "
        "its last good settings: llm.toml does not parse.")


async def test_a_new_lan_provider_is_added_and_its_model_is_hers_and_local(app):
    sk = _skill(app)
    ctx = await _ctx(app)
    assert "mac" not in app.cfg.settings.providers
    got, ask = await _answer(app, sk.propose(
        ctx, target="providers.mac",
        changes={"kind": "openai", "label": "My Mac", "base_url": "192.168.1.50:1234",
                 "models": {"small": {"caps": ["chat", "tools"]},
                            "qwen3:14b": {"label": "Qwen3 14B (Mac)", "caps": ["chat", "tools"],
                                          "context": 40960}}},
        why="A model server on my Mac for my agents."), True)
    assert not got.is_error, got.content
    assert ap.chat(ask, None)["line"] == "Celine asks to add the model provider mac."
    assert {"label": "Address", "after": "192.168.1.50:1234"} in ask.changes
    assert all("before" not in c for c in ask.changes), "a new provider had no values"
    assert {"label": "Models", "after": "small, chat and tools\n"
            "qwen3:14b, Qwen3 14B (Mac), chat and tools, 40,960 tokens"} in ask.changes
    s = app.cfg.settings
    assert s.providers["mac"]["base_url"] == "http://192.168.1.50:1234/v1"
    assert "mac/small" in her_models.allowed(s), "a template's model: takes it"
    assert not her_models.cloud(s, "mac/small"), "its sub-agents start without an ask"


async def test_a_new_providers_card_lists_the_roles_it_fills(app):
    from celine.settings.registry import ROLES
    sk = _skill(app)
    ctx = await _ctx(app)
    empty = [r for r in ROLES if not app.cfg.settings.chain(r)]
    got, ask = await _answer(app, sk.propose(
        ctx, target="providers.mac",
        changes={"kind": "openai", "label": "My Mac", "base_url": "192.168.1.50:1234",
                 "models": {"ears": {"caps": ["transcription"]}}},
        why="A model server on my Mac."), True)
    assert not got.is_error, got.content
    s = app.cfg.settings
    filled = [r for r in empty if s.chain(r) == ["mac/ears"]]
    assert filled == ["transcription"]
    for r in filled:
        assert {"label": ROLES[r]["label"], "after": "mac/ears"} in ask.changes
        assert f"{ROLES[r]['label']}: mac/ears." in got.content
    assert len([c for c in ask.changes if c["after"] == "mac/ears"]) == len(filled)
